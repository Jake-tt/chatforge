// Mermaid is large, so it loads on the first diagram rather than with the app.
type Mermaid = typeof import("mermaid").default;

let loaded: Promise<Mermaid> | null = null;
let seq = 0;

function load(): Promise<Mermaid> {
  loaded ??= import("mermaid").then(({ default: mermaid }) => {
    mermaid.initialize({
      startOnLoad: false,
      // The source is model output: no HTML labels, scripts or click handlers.
      securityLevel: "strict",
      theme: window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "default",
    });
    return mermaid;
  });
  return loaded;
}

/** The diagram as SVG markup; rejects if the source isn't a valid diagram. */
export async function renderDiagram(source: string): Promise<string> {
  const mermaid = await load();
  // Checked first: rendering invalid source leaves mermaid's error graphic in the page.
  if (!(await mermaid.parse(source, { suppressErrors: true }))) {
    throw new Error("invalid diagram");
  }
  seq += 1;
  const { svg } = await mermaid.render(`diagram-${seq}`, source);
  return svg;
}
