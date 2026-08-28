import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const source = new URL("./assets/riverbank-mark-1024.png", import.meta.url);
const output = new URL("./assets/riverbank-call.icns", import.meta.url);
const workspace = mkdtempSync(join(tmpdir(), "riverbank-icon-"));
const icons = [
  ["icp4", 16],
  ["icp5", 32],
  ["icp6", 64],
  ["ic07", 128],
  ["ic08", 256],
  ["ic09", 512],
  ["ic10", 1024],
];

try {
  const entries = icons.map(([type, size]) => {
    const png = join(workspace, `${size}.png`);
    execFileSync("/usr/bin/sips", [
      "-z",
      String(size),
      String(size),
      source.pathname,
      "--out",
      png,
    ], { stdio: "ignore" });

    const image = readFileSync(png);
    const header = Buffer.alloc(8);
    header.write(type, 0, 4, "ascii");
    header.writeUInt32BE(image.length + 8, 4);
    return Buffer.concat([header, image]);
  });

  const body = Buffer.concat(entries);
  const header = Buffer.alloc(8);
  header.write("icns", 0, 4, "ascii");
  header.writeUInt32BE(body.length + 8, 4);
  writeFileSync(output, Buffer.concat([header, body]));
  console.log(`Generated ${output.pathname}`);
} finally {
  rmSync(workspace, { recursive: true, force: true });
}
