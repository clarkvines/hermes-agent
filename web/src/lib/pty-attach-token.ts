const PTY_ATTACH_STATE_KEY = "hermes.pty.state.chat";
const LEGACY_PTY_ATTACH_TOKEN_KEY = "hermes.pty.token.chat";
const LEGACY_PTY_REPLACE_TOKEN_KEY = "hermes.pty.replace.chat";
const MAX_PENDING_REPLACEMENTS = 32;

interface TokenStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

interface StoredPtyAttachState {
  attach: string;
  replacements: string[];
}

export interface PtyAttachParams {
  attach: string;
  replace?: string;
}

function randomToken(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function normalizeReplacements(tokens: string[]): string[] {
  return Array.from(new Set(tokens.map((token) => token.trim()).filter(Boolean))).slice(
    -MAX_PENDING_REPLACEMENTS,
  );
}

function readStoredState(storage: TokenStorage | null): StoredPtyAttachState {
  try {
    const raw = storage?.getItem(PTY_ATTACH_STATE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<StoredPtyAttachState>;
      if (typeof parsed.attach === "string" && parsed.attach) {
        return {
          attach: parsed.attach,
          replacements: normalizeReplacements(
            Array.isArray(parsed.replacements)
              ? parsed.replacements.filter((token): token is string => typeof token === "string")
              : [],
          ),
        };
      }
    }

    // Migrate state written by earlier dashboard builds.
    const attach = storage?.getItem(LEGACY_PTY_ATTACH_TOKEN_KEY) ?? "";
    const replacements = normalizeReplacements(
      (storage?.getItem(LEGACY_PTY_REPLACE_TOKEN_KEY) ?? "").split(","),
    );
    return { attach, replacements };
  } catch {
    return { attach: "", replacements: [] };
  }
}

export function buildPtyAttachParams(
  storage: TokenStorage | null,
  rotate = false,
  mint: () => string = randomToken,
): PtyAttachParams {
  const stored = readStoredState(storage);
  let replacements = stored.replacements;

  if (stored.attach && !rotate) {
    replacements = replacements.filter((token) => token !== stored.attach);
    return replacements.length
      ? { attach: stored.attach, replace: replacements.join(",") }
      : { attach: stored.attach };
  }

  const attach = mint();
  if (rotate && stored.attach && stored.attach !== attach) {
    replacements = normalizeReplacements([
      ...replacements.filter((token) => token !== stored.attach),
      stored.attach,
    ]);
  }

  try {
    // Current token and every pending replacement are committed in one storage
    // record, avoiding partial writes that can forget an old process tree.
    storage?.setItem(PTY_ATTACH_STATE_KEY, JSON.stringify({ attach, replacements }));
  } catch {
    // The browser wrapper mirrors this write in memory before persistence.
  }

  return replacements.length
    ? { attach, replace: replacements.join(",") }
    : { attach };
}

export function createPtyAttachParamsBuilder(
  persistentStorage: TokenStorage | null,
  mint: () => string = randomToken,
): (rotate?: boolean) => PtyAttachParams {
  const memory = new Map<string, string>();
  const mirroredStorage: TokenStorage = {
    getItem(key) {
      if (memory.has(key)) return memory.get(key) ?? null;
      try {
        const value = persistentStorage?.getItem(key) ?? null;
        if (value !== null) memory.set(key, value);
        return value;
      } catch {
        return null;
      }
    },
    setItem(key, value) {
      // Memory is authoritative for this page lifetime, so reconnects remain
      // stable even when privacy settings reject persistent storage.
      memory.set(key, value);
      try {
        persistentStorage?.setItem(key, value);
      } catch {
        // In-memory continuity still prevents reconnect fanout.
      }
    },
  };

  return (rotate = false) => buildPtyAttachParams(mirroredStorage, rotate, mint);
}

let browserBuilder: ((rotate?: boolean) => PtyAttachParams) | null = null;

export function browserPtyAttachParams(rotate = false): PtyAttachParams {
  if (browserBuilder === null) {
    let storage: TokenStorage | null = null;
    try {
      storage = window.localStorage;
    } catch {
      // Accessing localStorage itself can throw in privacy-restricted contexts.
    }
    browserBuilder = createPtyAttachParamsBuilder(storage);
  }
  return browserBuilder(rotate);
}
