"""Attachments: uploaded documents become Markdown that rides on the user's turn.

Driven over HTTP. The answering model is a RecordingChatModel, so the tests read exactly
what the model was sent; conversion (MarkItDown) and storage are real.
"""

import io
import zipfile

import pytest
from langchain_core.messages import AIMessage

from app.services import attachments
from tests.fakes import RecordingChatModel, ScriptedChatModel


def _pdf(text: str | None) -> bytes:
    """A one-page PDF with `text` drawn in Helvetica, or a page with no text at all."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
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
    convert = attachments.to_markdown

    def counting(data: bytes, extension: str) -> str:
        calls.append(extension)
        return convert(data, extension)

    monkeypatch.setattr(attachments, "to_markdown", counting)
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
