import { describe, expect, it } from "vitest";

import {
  buildPtyAttachParams,
  createPtyAttachParamsBuilder,
} from "./pty-attach-token";

const TOKEN_KEY = "hermes.pty.token.chat";
const REPLACE_KEY = "hermes.pty.replace.chat";

function memoryStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
  };
}

describe("buildPtyAttachParams", () => {
  it("reuses the stored token for refresh reattachment", () => {
    const storage = memoryStorage({ [TOKEN_KEY]: "existing" });

    expect(buildPtyAttachParams(storage, false, () => "new")).toEqual({
      attach: "existing",
    });
  });

  it("rotates a fresh chat and asks the server to close every previous PTY", () => {
    const storage = memoryStorage({ [TOKEN_KEY]: "old" });

    expect(buildPtyAttachParams(storage, true, () => "new")).toEqual({
      attach: "new",
      replace: "old",
    });
    expect(buildPtyAttachParams(storage, false, () => "unused")).toEqual({
      attach: "new",
      replace: "old",
    });
    expect(buildPtyAttachParams(storage, true, () => "newest")).toEqual({
      attach: "newest",
      replace: "old,new",
    });
    expect(buildPtyAttachParams(storage, false, () => "unused")).toEqual({
      attach: "newest",
      replace: "old,new",
    });
  });

  it("retries a pending replacement after a failed connection attempt", () => {
    const storage = memoryStorage({
      [TOKEN_KEY]: "new",
      [REPLACE_KEY]: "old",
    });

    expect(buildPtyAttachParams(storage, false, () => "unused")).toEqual({
      attach: "new",
      replace: "old",
    });
  });

  it("mints a first token without a replacement", () => {
    const storage = memoryStorage();

    expect(buildPtyAttachParams(storage, false, () => "first")).toEqual({
      attach: "first",
    });
  });

  it("still mints when browser storage is blocked", () => {
    const storage = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("blocked");
      },
    };

    const minted = ["first", "second"];
    const build = createPtyAttachParamsBuilder(storage, () => minted.shift() ?? "extra");

    expect(build(false)).toEqual({ attach: "first" });
    expect(build(false)).toEqual({ attach: "first" });
    expect(build(true)).toEqual({
      attach: "second",
      replace: "first",
    });
    expect(build(false)).toEqual({
      attach: "second",
      replace: "first",
    });
  });
});
