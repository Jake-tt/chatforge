import { describe, expect, it } from "vitest";
import { SSEParser } from "./sse";

const enc = new TextEncoder();

describe("SSEParser", () => {
  it("parses complete events", () => {
    const p = new SSEParser();
    const events = p.push(enc.encode('event: delta\ndata: {"text":"hi"}\n\nevent: done\ndata: {}\n\n'));
    expect(events).toEqual([
      { event: "delta", data: { text: "hi" } },
      { event: "done", data: {} },
    ]);
  });

  it("buffers events split across chunks", () => {
    const p = new SSEParser();
    expect(p.push(enc.encode('event: delta\ndata: {"te'))).toEqual([]);
    expect(p.push(enc.encode('xt":"a"}\n'))).toEqual([]);
    expect(p.push(enc.encode("\n"))).toEqual([{ event: "delta", data: { text: "a" } }]);
  });

  it("handles a multi-byte character split between chunks", () => {
    const bytes = enc.encode('event: delta\ndata: {"text":"안녕"}\n\n');
    const p = new SSEParser();
    const cut = bytes.indexOf(0xec) + 1; // inside the first Hangul syllable
    expect(p.push(bytes.slice(0, cut))).toEqual([]);
    expect(p.push(bytes.slice(cut))).toEqual([{ event: "delta", data: { text: "안녕" } }]);
  });

  it("ignores comments and accepts CRLF", () => {
    const p = new SSEParser();
    expect(p.push(enc.encode(': ping\r\n\r\nevent: done\r\ndata: {"ok":true}\r\n\r\n'))).toEqual([
      { event: "done", data: { ok: true } },
    ]);
  });
});
