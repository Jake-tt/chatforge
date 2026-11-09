import { describe, expect, it } from "vitest";
import type { AttachmentStatus } from "./api";
import { waitUntilConverted } from "./attachments";

const status = (s: AttachmentStatus["status"], error_code: string | null = null): AttachmentStatus => ({
  id: "a1",
  filename: "notes.md",
  kind: "document",
  status: s,
  chars: s === "ready" ? 12 : null,
  error_code,
});

const noWait = async () => undefined;

describe("waitUntilConverted", () => {
  it("polls until the background conversion is ready", async () => {
    const answers = [status("processing"), status("processing"), status("ready")];
    let calls = 0;
    const result = await waitUntilConverted(async () => answers[calls++], { sleep: noWait });
    expect(result.status).toBe("ready");
    expect(calls).toBe(3);
  });

  it("stops at a failure and reports it", async () => {
    const result = await waitUntilConverted(async () => status("failed", "no_text"), {
      sleep: noWait,
    });
    expect([result.status, result.error_code]).toEqual(["failed", "no_text"]);
  });

  it("gives up after its time budget", async () => {
    const result = await waitUntilConverted(async () => status("processing"), {
      sleep: noWait,
      tries: 3,
    });
    expect([result.status, result.error_code]).toEqual(["failed", "conversion_timeout"]);
  });
});
