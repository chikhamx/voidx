import {
  getThemePreference,
  setThemePreference,
  resolveTheme,
  applyTheme,
  initTheme,
  toggleTheme,
  _resetThemeForTest,
} from "../../src/ui/theme";

describe("theme", () => {
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute("data-theme");
    _resetThemeForTest();
  });

  it("defaults to dark when no preference has been stored", () => {
    expect(getThemePreference()).toBe("dark");
  });

  it("persists explicit preference to localStorage", () => {
    setThemePreference("dark");
    expect(window.localStorage.getItem("voidx.theme")).toBe("dark");
    expect(getThemePreference()).toBe("dark");
  });

  it("resolves explicit preference directly", () => {
    expect(resolveTheme("light")).toBe("light");
    expect(resolveTheme("dark")).toBe("dark");
  });

  it.each([
    [true, "dark"],
    [false, "light"],
  ] as const)("resolves system dark=%s to %s", (matches, expected) => {
    const original = window.matchMedia;
    window.matchMedia = (() => ({ matches })) as typeof window.matchMedia;
    expect(resolveTheme("system")).toBe(expected);
    window.matchMedia = original;
  });

  it("applyTheme writes data-theme on documentElement", () => {
    const resolved = applyTheme("dark");
    expect(resolved).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("toggleTheme flips the current resolved theme and persists it", () => {
    applyTheme("light");
    const next = toggleTheme();
    expect(next).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(getThemePreference()).toBe("dark");

    toggleTheme();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(getThemePreference()).toBe("light");
  });

  it("initTheme applies the stored preference", () => {
    window.localStorage.setItem("voidx.theme", "dark");
    initTheme();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("falls back to dark for invalid stored values", () => {
    window.localStorage.setItem("voidx.theme", "neon");
    expect(getThemePreference()).toBe("dark");
  });
});
