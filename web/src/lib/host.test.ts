import { afterEach, describe, expect, it } from "vitest";

import { getCliServerUrl, setCodifyHostConfig } from "./host";

afterEach(() => {
  setCodifyHostConfig({});
});

describe("getCliServerUrl", () => {
  it("returns window.location.origin when no suffix is configured", () => {
    setCodifyHostConfig({});
    const url = getCliServerUrl();
    expect(url).toBe(window.location.origin);
  });

  it("appends the configured cliServerUrlSuffix", () => {
    setCodifyHostConfig({ cliServerUrlSuffix: "/api/2.0/codify" });
    const url = getCliServerUrl();
    expect(url).toBe(`${window.location.origin}/api/2.0/codify`);
  });

  it("handles an empty string suffix the same as no suffix", () => {
    setCodifyHostConfig({ cliServerUrlSuffix: "" });
    expect(getCliServerUrl()).toBe(window.location.origin);
  });
});
