export interface SSEEvent {
  event: string;
  data: unknown;
}

/**
 * Incremental Server-Sent Events parser.
 *
 * EventSource only supports GET, but sending a message is a POST, so the response body is
 * read with fetch + ReadableStream. Network chunks can split an event anywhere (even inside
 * a multi-byte character), so bytes are decoded in streaming mode and only complete
 * `\n\n`-terminated blocks are emitted.
 */
export class SSEParser {
  private buffer = "";
  private decoder = new TextDecoder();

  push(chunk: Uint8Array): SSEEvent[] {
    this.buffer += this.decoder.decode(chunk, { stream: true }).replace(/\r\n/g, "\n");
    const events: SSEEvent[] = [];
    let boundary: number;
    while ((boundary = this.buffer.indexOf("\n\n")) !== -1) {
      const block = this.buffer.slice(0, boundary);
      this.buffer = this.buffer.slice(boundary + 2);
      const parsed = parseBlock(block);
      if (parsed) events.push(parsed);
    }
    return events;
  }
}

function parseBlock(block: string): SSEEvent | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // comment / keep-alive
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (data.length === 0) return null;
  return { event, data: JSON.parse(data.join("\n")) };
}
