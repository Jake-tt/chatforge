import type { AttachmentStatus } from "./api";

// An upload is read in the background: ask until it's ready or failed, but not forever.
export async function waitUntilConverted(
  fetchStatus: () => Promise<AttachmentStatus>,
  {
    tries = 120,
    intervalMs = 1000,
    sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
  }: { tries?: number; intervalMs?: number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<AttachmentStatus> {
  let last: AttachmentStatus | null = null;
  for (let i = 0; i < tries; i++) {
    last = await fetchStatus();
    if (last.status !== "processing") return last;
    await sleep(intervalMs);
  }
  return { ...(last as AttachmentStatus), status: "failed", error_code: "conversion_timeout" };
}
