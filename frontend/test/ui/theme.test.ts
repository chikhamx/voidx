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

  it("defaults to system preference", () => {
    expect(getThemePreference()).toBe("system");
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

  it("resolves system preference to a concrete theme", () => {
    const resolved = resolveTheme("system");
    expect(["light", "dark"]).toContain(resolved);
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

  it("ignores invalid stored values", () => {
    window.localStorage.setItem("voidx.theme", "neon");
    expect(getThemePreference()).toBe("system");
  });
});
