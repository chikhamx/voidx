import { iconSvg, ICON_NAMES } from "../../src/utils/icons";

describe("icons", () => {
  it("provides a non-trivial icon set", () => {
    expect(ICON_NAMES.length).toBeGreaterThan(30);
  });

  it("generates a normalized svg string", () => {
    const svg = iconSvg("search");
    expect(svg).toContain("<svg");
    expect(svg).toContain('class="vx-icon"');
    expect(svg).toContain('viewBox="0 0 24 24"');
    expect(svg).toContain('stroke="currentColor"');
    expect(svg).toContain('stroke-width="1.6"');
    expect(svg).toContain('aria-hidden="true"');
  });

  it("respects custom size and stroke width", () => {
    const svg = iconSvg("plus", 14, 2);
    expect(svg).toContain('width="14"');
    expect(svg).toContain('height="14"');
    expect(svg).toContain('stroke-width="2"');
  });


  it("every icon renders non-empty path content", () => {
    for (const name of ICON_NAMES) {
      expect(iconSvg(name).length).toBeGreaterThan(100);
    }
  });
});
