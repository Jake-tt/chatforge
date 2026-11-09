"""Attachments: uploaded documents become Markdown that rides on the user's turn.

Driven over HTTP. The answering model is a RecordingChatModel, so the tests read exactly
what the model was sent; conversion (MarkItDown) and storage are real.
"""

import io
import zipfile

import pytest
from langchain_core.messages import AIMessage

from app.services import convert
from tests.fakes import RecordingChatModel, ScriptedChatModel


def _pdf(text: str | None, pages: int = 1) -> bytes:
    """A PDF whose pages draw `text` in Helvetica, or pages with no text (like a scan)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    # 1 catalog, 2 pages, 3 font, 4 contents, then one page object per page.
    kids = " ".join(f"{5 + i} 0 R" for i in range(pages)).encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % pages,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        *[b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
          b"/Resources << /Font << /F1 3 0 R >> >> >>"] * pages,
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, xref))
    return out.getvalue()


def _docx(paragraph: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="rels" ContentType="'
                   'application/vnd.openxmlformats-package.relationships+xml"/><Default '
                   'Extension="xml" ContentType="application/xml"/><Override PartName="'
                   '/word/document.xml" ContentType="application/vnd.openxmlformats-'
                   'officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats'
                   '.org/package/2006/relationships"><Relationship Id="rId1" Type="http://'
                   'schemas.openxmlformats.org/officeDocument/2006/relationships/'
                   'officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml",
                   '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats'
                   f'.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{paragraph}'
                   '</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


@pytest.fixture
async def setup(make_harness, tmp_path):
    async def _make(**settings):
        model = RecordingChatModel()
        h = await make_harness(chat_model=model, attachments_dir=str(tmp_path / "blobs"),
                               **settings)
        headers = await h.guest()
        cid = await h.new_conversation(headers)
        return h, model, headers, cid
    return _make


async def _upload(h, headers, cid, name: str, data: bytes):
    return await h.client.post(f"/conversations/{cid}/attachments", headers=headers,
                               files={"file": (name, data, "application/octet-stream")})


async def _send(h, headers, cid, text: str, ids: list[str]):
    return await h.client.post(f"/conversations/{cid}/messages", headers=headers,
                               json={"content": text, "attachment_ids": ids})


async def test_a_document_rides_on_the_turn_that_carries_it(setup):
    h, model, headers, cid = await setup()

    up = await _upload(h, headers, cid, "notes.md", "# 회의록\n\n예산은 300만 원".encode())
    assert up.status_code == 201
    assert up.json() | {"id": "x"} == {"id": "x", "filename": "notes.md", "kind": "document",
                                       "chars": len("# 회의록\n\n예산은 300만 원")}
    assert (await _send(h, headers, cid, "예산이 얼마야?", [up.json()["id"]])).status_code == 200

    sent = model.current_turns[-1]
    # Uploaded text is material, not instructions; the user's own words come last.
    assert '<attachment name="notes.md">' in sent and "예산은 300만 원" in sent
    assert "not instructions" in sent
    assert "by file name, not by [n]" in sent  # [n] is for retrieved sources
    assert sent.endswith("예산이 얼마야?")


async def test_an_attachment_is_resent_byte_for_byte_on_later_turns(setup):
    h, model, headers, cid = await setup()
    up = await _upload(h, headers, cid, "notes.md", "예산은 300만 원".encode())
    await _send(h, headers, cid, "예산이 얼마야?", [up.json()["id"]])
    first = model.current_turns[-1]

    await h.chat(headers, cid, "고마워")

    # The earlier turn is replayed exactly as it was sent, so the prompt cache still hits.
    assert model.turns()[0] == ("user", first)


async def test_pdf_and_docx_files_become_markdown(setup):
    h, model, headers, cid = await setup()
    pdf = await _upload(h, headers, cid, "report.pdf", _pdf("Quarterly revenue grew 12%"))
    docx = await _upload(h, headers, cid, "plan.docx", _docx("Launch in March"))
    assert (pdf.status_code, docx.status_code) == (201, 201)

    await _send(h, headers, cid, "요약해줘", [pdf.json()["id"], docx.json()["id"]])

    sent = model.current_turns[-1]
    assert "Quarterly revenue grew 12%" in sent and "Launch in March" in sent


async def test_the_same_file_is_converted_only_once(setup, monkeypatch):
    calls: list[str] = []
    original = convert.to_markdown

    def counting(data: bytes, extension: str) -> str:
        calls.append(extension)
        return original(data, extension)

    monkeypatch.setattr(convert, "to_markdown", counting)
    h, model, headers, cid = await setup()
    data = _docx("Launch in March")

    first = await _upload(h, headers, cid, "plan.docx", data)
    again = await _upload(h, headers, cid, "plan-copy.docx", data)

    assert (first.status_code, again.status_code) == (201, 201)
    assert calls == [".docx"]
    assert again.json()["id"] != first.json()["id"]


@pytest.mark.parametrize(("name", "data", "status", "code"), [
    ("tool.exe", b"MZ\x90\x00binary", 415, "unsupported_file"),
    ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, 415, "images_not_supported"),
    ("scan.pdf", _pdf(None), 422, "no_text"),
    ("empty.txt", b"", 422, "no_text"),
])
async def test_files_that_cannot_be_read_are_refused(setup, name, data, status, code):
    h, model, headers, cid = await setup()

    r = await _upload(h, headers, cid, name, data)

    assert (r.status_code, r.json()["detail"]["code"]) == (status, code)


async def test_oversized_files_and_zip_bombs_are_refused(setup):
    h, model, headers, cid = await setup(attachment_max_bytes=2_000,
                                         attachment_max_unzipped_bytes=50_000)
    big = await _upload(h, headers, cid, "big.txt", b"a" * 2_001)

    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "0" * 100_000)  # compresses to a few hundred bytes
    small_bomb = await _upload(h, headers, cid, "bomb.docx", bomb.getvalue())

    assert (big.status_code, big.json()["detail"]["code"]) == (413, "file_too_large")
    assert (small_bomb.status_code, small_bomb.json()["detail"]["code"]) == (413, "file_too_large")


async def test_a_long_document_is_cut_for_the_model_and_says_so(setup):
    h, model, headers, cid = await setup(attachment_inline_chars=40)
    up = await _upload(h, headers, cid, "long.txt", ("가나다라마바사 " * 50).encode())

    await _send(h, headers, cid, "읽어줘", [up.json()["id"]])

    sent = model.current_turns[-1]
    assert "(cut: " in sent and "가나다라마바사 " * 50 not in sent


async def test_attachments_only_go_with_their_own_conversation_once(setup):
    h, model, headers, cid = await setup()
    other = await h.new_conversation(headers)
    up = (await _upload(h, headers, cid, "notes.md", b"budget")).json()["id"]

    elsewhere = await _send(h, headers, other, "읽어줘", [up])
    assert elsewhere.status_code == 400
    assert elsewhere.json()["detail"]["code"] == "invalid_attachment"

    assert (await _send(h, headers, cid, "읽어줘", [up])).status_code == 200
    reused = await _send(h, headers, cid, "다시 읽어줘", [up])
    assert (reused.status_code, reused.json()["detail"]["code"]) == (400, "invalid_attachment")

    stranger = await h.guest()
    assert (await _upload(h, stranger, cid, "x.md", b"x")).status_code == 404


async def test_a_message_lists_its_attachments(setup):
    h, model, headers, cid = await setup()
    up = (await _upload(h, headers, cid, "notes.md", b"budget")).json()["id"]
    await _send(h, headers, cid, "읽어줘", [up])

    first = (await h.client.get(f"/conversations/{cid}", headers=headers)).json()["messages"][0]

    assert first["attachments"] == [{"id": up, "filename": "notes.md", "kind": "document"}]


async def test_at_most_four_attachments_per_message(setup):
    h, model, headers, cid = await setup()
    ids = [(await _upload(h, headers, cid, f"n{i}.md", b"x%d" % i)).json()["id"]
           for i in range(5)]

    assert (await _send(h, headers, cid, "읽어줘", ids)).status_code == 422


async def test_a_turn_that_fails_before_any_answer_frees_its_attachments(make_harness, tmp_path):
    # The server undoes such a turn so the question can be sent again; its files too.
    model = ScriptedChatModel(script=[ConnectionError("model down"), AIMessage("읽었어요")])
    h = await make_harness(chat_model=model, attachments_dir=str(tmp_path))
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    up = (await _upload(h, headers, cid, "notes.md", b"budget")).json()["id"]

    failed = await _send(h, headers, cid, "읽어줘", [up])
    assert "error" in failed.text
    again = await _send(h, headers, cid, "읽어줘", [up])

    assert again.status_code == 200 and "읽었어요" in again.text


async def test_uploads_are_rate_limited(setup):
    h, model, headers, cid = await setup(uploads_per_minute=2)

    codes = [(await _upload(h, headers, cid, f"n{i}.md", b"x%d" % i)).status_code
             for i in range(3)]

    assert codes == [201, 201, 429]


async def test_a_summary_still_knows_a_file_was_attached(make_harness, tmp_path):
    from tests.test_summary import SUMMARY_ON, ScriptedProvider

    llm = ScriptedProvider()
    h = await make_harness(llm=llm, chat_model=llm.answers, attachments_dir=str(tmp_path),
                           **SUMMARY_ON)
    headers = await h.guest()
    cid = await h.new_conversation(headers)
    up = (await _upload(h, headers, cid, "budget.md", b"budget")).json()["id"]
    await _send(h, headers, cid, "읽어줘", [up])
    for message in ("two", "three"):
        await h.chat(headers, cid, message)
    await h.ctx.tasks.drain()

    assert "[attached: budget.md]" in llm.summary_prompts[0]


# --- images: only with a vision model ------------------------------------------------

def _png(width: int = 40, height: int = 30, color=(200, 30, 30)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def _jpeg_with_location() -> bytes:
    from PIL import Image

    image = Image.new("RGB", (40, 30), (10, 120, 10))
    exif = Image.Exif()
    exif[0x8825] = {2: (37.0, 33.0, 0.0)}  # GPSInfo: where the photo was taken
    out = io.BytesIO()
    image.save(out, "JPEG", exif=exif)
    return out.getvalue()


def _image_blocks(model: RecordingChatModel, call: int = -1) -> list[list[dict]]:
    """Per user message of one call, its image content blocks."""
    from langchain_core.messages import HumanMessage

    return [[b for b in m.content if isinstance(b, dict) and b.get("type") == "image"]
            if isinstance(m.content, list) else []
            for m in model.calls[call]["messages"] if isinstance(m, HumanMessage)]


async def test_an_image_is_described_once_and_sent_with_its_turn(setup):
    h, model, headers, cid = await setup(vision_enabled=True)
    used_before = (await h.client.get("/usage", headers=headers)).json()["used"]

    up = await _upload(h, headers, cid, "chart.png", _png())
    assert up.status_code == 201 and up.json()["kind"] == "image"
    # Describing it is work the user asked for by uploading: charged like any reply.
    assert (await h.client.get("/usage", headers=headers)).json()["used"] > used_before
    await _send(h, headers, cid, "이 그림 설명해줘", [up.json()["id"]])

    sent = model.current_turns[-1]
    assert '<attachment name="chart.png">' in sent and "(fake) image" in sent
    assert len(_image_blocks(model)[-1]) == 1


async def test_an_image_is_sent_whole_for_three_turns_then_as_its_description(setup):
    h, model, headers, cid = await setup(vision_enabled=True)
    up = (await _upload(h, headers, cid, "chart.png", _png())).json()["id"]
    await _send(h, headers, cid, "이 그림 설명해줘", [up])

    for message in ("하나", "둘"):
        await h.chat(headers, cid, message)
    assert len(_image_blocks(model)[0]) == 1  # the third user turn back: still whole

    await h.chat(headers, cid, "셋")
    assert _image_blocks(model)[0] == []  # older: its description stands in
    assert "(fake) image" in model.turns()[0][1]


async def test_images_are_refused_without_a_vision_model(setup):
    h, model, headers, cid = await setup()  # the fake provider, like a local model

    r = await _upload(h, headers, cid, "chart.png", _png())

    assert (r.status_code, r.json()["detail"]["code"]) == (415, "images_not_supported")


async def test_a_large_image_is_scaled_down_and_its_location_dropped(setup, tmp_path):
    from PIL import Image

    h, model, headers, cid = await setup(vision_enabled=True)
    big = await _upload(h, headers, cid, "wide.png", _png(3000, 600))
    photo = await _upload(h, headers, cid, "photo.jpg", _jpeg_with_location())
    assert (big.status_code, photo.status_code) == (201, 201)

    stored = {p.stat().st_size: Image.open(p) for p in (tmp_path / "blobs").rglob("*")
              if p.is_file()}
    assert max(max(i.size) for i in stored.values()) <= 1568
    assert all(0x8825 not in i.getexif() for i in stored.values())


async def test_a_file_that_only_looks_like_an_image_is_refused(setup):
    h, model, headers, cid = await setup(vision_enabled=True)

    fake = await _upload(h, headers, cid, "x.png", b"\x89PNG\r\n\x1a\n" + b"junk" * 10)

    assert (fake.status_code, fake.json()["detail"]["code"]) == (422, "unreadable_file")



# --- scanned PDFs: read page by page by a vision model --------------------------------

async def test_a_scanned_pdf_is_read_page_by_page_by_a_vision_model(setup):
    h, model, headers, cid = await setup(vision_enabled=True)
    used = (await h.client.get("/usage", headers=headers)).json()["used"]

    up = await _upload(h, headers, cid, "scan.pdf", _pdf(None, pages=2))

    assert up.status_code == 201 and up.json()["kind"] == "document"
    await _send(h, headers, cid, "요약해줘", [up.json()["id"]])
    sent = model.current_turns[-1]
    assert "## Page 1" in sent and "## Page 2" in sent and "(fake) image: image/jpeg" in sent
    # Reading the pages is work the user asked for by uploading: charged.
    assert (await h.client.get("/usage", headers=headers)).json()["used"] > used


async def test_only_the_first_pages_of_a_long_scan_are_read(setup):
    h, model, headers, cid = await setup(vision_enabled=True, ocr_max_pages=2)

    up = await _upload(h, headers, cid, "scan.pdf", _pdf(None, pages=5))
    await _send(h, headers, cid, "요약해줘", [up.json()["id"]])

    sent = model.current_turns[-1]
    assert "## Page 2" in sent and "## Page 3" not in sent
    assert "pages 3-5 were not read" in sent


async def test_a_single_unread_page_is_named_as_one(setup):
    h, model, headers, cid = await setup(vision_enabled=True, ocr_max_pages=2)

    up = await _upload(h, headers, cid, "scan.pdf", _pdf(None, pages=3))
    await _send(h, headers, cid, "요약해줘", [up.json()["id"]])

    assert "(page 3 was not read)" in model.current_turns[-1]


async def test_a_pdf_with_text_is_not_sent_to_the_vision_model(setup, monkeypatch):
    seen: list[int] = []
    original = convert.render_pages

    def counting(data: bytes, max_side: int, limit: int):
        seen.append(limit)
        return original(data, max_side, limit)

    monkeypatch.setattr(convert, "render_pages", counting)
    h, model, headers, cid = await setup(vision_enabled=True)

    up = await _upload(h, headers, cid, "report.pdf", _pdf("Quarterly revenue grew 12%"))

    assert up.status_code == 201 and seen == []


# --- long documents: what was cut is found by search_attachments ----------------------

def _long_document(fact: str) -> bytes:
    filler = "\n\n".join(f"{i}장. 회의에서 일정과 예산을 다시 검토했다." for i in range(60))
    return f"# 프로젝트 문서\n\n{filler}\n\n## 부록\n\n{fact}".encode()


async def _agent_setup(make_harness, tmp_path, script: list):
    agent = ScriptedChatModel(script=script)
    h = await make_harness(chat_model=agent, attachments_dir=str(tmp_path),
                           attachment_inline_chars=200)
    headers = await h.guest()
    return h, agent, headers


async def test_a_cut_document_is_searched_for_the_part_not_shown(make_harness, tmp_path):
    from tests.fakes import tool_call

    h, agent, headers = await _agent_setup(make_harness, tmp_path, [
        tool_call("search_attachments", query="출입 비밀번호"), AIMessage("ZEBRA-42예요.")])
    cid = await h.new_conversation(headers)
    manual = _long_document("출입 비밀번호는 ZEBRA-42이다.")
    up = await _upload(h, headers, cid, "manual.md", manual)

    r = await _send(h, headers, cid, "출입 비밀번호가 뭐야?", [up.json()["id"]])

    first_call = agent.calls[0]
    assert "search_attachments" in [t["function"]["name"] for t in first_call["tools"]]
    assert "ZEBRA-42" not in first_call["messages"][-1].content  # cut from the inline copy
    tool_result = agent.calls[1]["messages"][-1].content
    assert "ZEBRA-42" in tool_result and "manual.md" in tool_result
    assert "ZEBRA-42예요." in r.text


async def test_attachment_search_is_offered_only_when_a_document_was_cut(make_harness, tmp_path):
    h, agent, headers = await _agent_setup(make_harness, tmp_path, [AIMessage("네")])
    cid = await h.new_conversation(headers)
    up = await _upload(h, headers, cid, "short.md", "짧은 메모".encode())

    await _send(h, headers, cid, "읽어줘", [up.json()["id"]])

    assert "search_attachments" not in [t["function"]["name"] for t in agent.calls[0]["tools"]]


async def test_attachment_search_stays_within_its_conversation(make_harness, tmp_path):
    from tests.fakes import tool_call

    h, agent, headers = await _agent_setup(make_harness, tmp_path, [
        AIMessage("네"), tool_call("search_attachments", query="출입 비밀번호"),
        AIMessage("모르겠어요")])
    elsewhere = await h.new_conversation(headers)
    secret = await _upload(h, headers, elsewhere, "manual.md",
                           _long_document("출입 비밀번호는 ZEBRA-42이다."))
    await _send(h, headers, elsewhere, "읽어둬", [secret.json()["id"]])
    cid = await h.new_conversation(headers)
    mine = await _upload(h, headers, cid, "other.md", _long_document("주차는 B2층이다."))

    await _send(h, headers, cid, "출입 비밀번호가 뭐야?", [mine.json()["id"]])

    assert "ZEBRA-42" not in agent.calls[-1]["messages"][-1].content
