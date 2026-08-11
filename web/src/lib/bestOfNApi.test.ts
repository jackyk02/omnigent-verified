import { describe, expect, it } from "vitest";
import {
  type BestOfNConfig,
  bestOfNConfigFromWire,
  bestOfNConfigResponseFromWire,
  bestOfNConfigToWire,
} from "./bestOfNApi";

// A wire payload shaped exactly like the server's GET /v1/best-of-n/config
// `config` field, exercising both filled and null optional fields.
const EMPTY_ENV: Record<string, string> = {};
const WIRE_CONFIG = {
  proposers: [
    { harness: "claude", enabled: true, count: 2, model: "opus", command: null, env: { A: "1" } },
    {
      harness: "codex",
      enabled: false,
      count: 1,
      model: null,
      command: "codex --yolo",
      env: EMPTY_ENV,
    },
    { harness: "pi", enabled: true, count: 1, model: null, command: null, env: EMPTY_ENV },
  ],
  proposal_model: "deepseek-v4-flash",
  criteria: {
    Correctness: "Compare the final change...",
    "Task Completeness": "Did it finish?",
  },
  verifier: {
    model: "deepseek-v4-flash",
    base_url: "https://api.deepseek.com/v1",
    api_key_env: "DEEPSEEK_API_KEY",
    n_evaluations: 4,
    pivots: 2,
    seed: 0,
    max_workers: 16,
    on_error: "tie" as const,
  },
  proposal_timeout_s: 900.0,
  keep_proposal_branches: false,
};

describe("bestOfNConfigFromWire", () => {
  it("converts snake_case wire fields to the camelCase surface", () => {
    const config = bestOfNConfigFromWire(WIRE_CONFIG);
    expect(config.proposalModel).toBe("deepseek-v4-flash");
    expect(config.proposalTimeoutS).toBe(900.0);
    expect(config.keepProposalBranches).toBe(false);
    expect(config.verifier).toEqual({
      model: "deepseek-v4-flash",
      baseUrl: "https://api.deepseek.com/v1",
      apiKeyEnv: "DEEPSEEK_API_KEY",
      nEvaluations: 4,
      pivots: 2,
      seed: 0,
      maxWorkers: 16,
      onError: "tie",
    });
    expect(config.proposers[0]).toEqual({
      harness: "claude",
      enabled: true,
      count: 2,
      model: "opus",
      command: null,
      env: { A: "1" },
    });
  });

  it("defaults absent optional proposer fields", () => {
    // Older/lean server payloads may omit model/command/env entirely; the
    // camelCase surface must still present them as null / {}.
    const config = bestOfNConfigFromWire({
      ...WIRE_CONFIG,
      proposers: [{ harness: "claude", enabled: true, count: 1 }],
    });
    expect(config.proposers[0]).toEqual({
      harness: "claude",
      enabled: true,
      count: 1,
      model: null,
      command: null,
      env: {},
    });
  });

  it("preserves criteria insertion order", () => {
    const config = bestOfNConfigFromWire(WIRE_CONFIG);
    // Order matters: the verifier judges criteria in the order configured,
    // and the settings UI renders rows in this order.
    expect(Object.keys(config.criteria)).toEqual(["Correctness", "Task Completeness"]);
  });
});

describe("bestOfNConfigToWire", () => {
  it("round-trips: fromWire → toWire reproduces the wire payload", () => {
    const config: BestOfNConfig = bestOfNConfigFromWire(WIRE_CONFIG);
    expect(bestOfNConfigToWire(config)).toEqual(WIRE_CONFIG);
  });
});

describe("bestOfNConfigResponseFromWire", () => {
  it("converts the full envelope, keeping harness metadata", () => {
    const response = bestOfNConfigResponseFromWire({
      object: "best_of_n_config",
      config: WIRE_CONFIG,
      verifier_key_present: false,
      known_harnesses: ["claude", "codex", "pi"],
      harness_availability: { claude: true, codex: true, pi: false },
      default_criteria: { Correctness: "Compare the final change..." },
    });
    expect(response.knownHarnesses).toEqual(["claude", "codex", "pi"]);
    expect(response.harnessAvailability).toEqual({ claude: true, codex: true, pi: false });
    expect(response.verifierKeyPresent).toBe(false);
    expect(response.defaultCriteria).toEqual({ Correctness: "Compare the final change..." });
    expect(response.config.verifier.maxWorkers).toBe(16);
  });

  it("defaults verifier_key_present to true when the server omits it", () => {
    const response = bestOfNConfigResponseFromWire({
      object: "best_of_n_config",
      config: WIRE_CONFIG,
      known_harnesses: ["claude"],
      harness_availability: { claude: true },
      default_criteria: {},
    });
    // Fail open: an older server can't report the key, and the launch form
    // must not block on a field that doesn't exist.
    expect(response.verifierKeyPresent).toBe(true);
  });
});
