import json
import shutil
import subprocess
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITE_JS = ROOT / "apps/paper-radar/static/site.js"


class PaperRadarTypographyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for the browser typography fixture")
    def test_inline_scripts_are_rendered_as_semantic_sub_and_sup_elements(self) -> None:
        script = textwrap.dedent(
            f"""
            const fs = require("fs");
            const vm = require("vm");
            const makeNode = (tag, text = "") => ({{
              tag, text, children: [], attrs: {{}}, className: "",
              append(...items) {{ this.children.push(...items); }},
              setAttribute(key, value) {{ this.attrs[key] = value; }},
              replaceChildren(...items) {{ this.children = items; }},
              set textContent(value) {{ this.text = String(value); this.children = []; }},
              get textContent() {{
                return this.children.length
                  ? this.children.map((item) => item.textContent || item.text || "").join("")
                  : this.text;
              }},
            }});
            const sandbox = {{ document: {{ querySelector: () => null, querySelectorAll: () => [] }} }};
            vm.createContext(sandbox);
            const source = fs.readFileSync({json.dumps(str(SITE_JS))}, "utf8");
            vm.runInContext(source + "\\nglobalThis.testRender = renderReadableMath;", sandbox);
            const documentRef = {{
              createDocumentFragment: () => makeNode("#fragment"),
              createTextNode: (text) => makeNode("#text", text),
              createElement: (tag) => makeNode(tag),
            }};
            const target = makeNode("p");
            target.ownerDocument = documentRef;
            sandbox.testRender(target, "Δ(s_k) = (f_L, f^l, f^r)，以及 x_i^2");
            const nodes = [];
            const visit = (item) => {{ nodes.push(item); item.children.forEach(visit); }};
            target.children.forEach(visit);
            console.log(JSON.stringify({{
              text: target.textContent,
              scripts: nodes
                .filter((item) => item.tag === "sup" || item.tag === "sub")
                .map((item) => [item.tag, item.textContent]),
            }}));
            """
        )
        result = subprocess.run(
            [shutil.which("node") or "node", "-e", script],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        rendered = json.loads(result.stdout)
        self.assertEqual(rendered["text"], "Δ(sk) = (fL, fl, fr)，以及 xi2")
        self.assertEqual(
            rendered["scripts"],
            [["sub", "k"], ["sub", "L"], ["sup", "l"], ["sup", "r"], ["sub", "i"], ["sup", "2"]],
        )


if __name__ == "__main__":
    unittest.main()
