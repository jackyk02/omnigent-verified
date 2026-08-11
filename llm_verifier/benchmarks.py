"""Benchmark registry.

Each benchmark is a typed `Benchmark` rather than a YAML file: defaults live in
one place, a bad field is a type/attribute error instead of a silent `None`, and
paths/criteria are checked by your editor. `run.py <name>` looks the name up in
`BENCHMARKS`; the CLI flags (`--pivots`, `--n-evaluations`, `--seed`) still
override the values here at launch time.
"""

from dataclasses import dataclass, field


@dataclass
class Benchmark:
    name: str  # human-readable title shown in the report
    loader: str  # key into llm_verifier.loaders.LOADERS
    prompts: str  # criteria name (criteria/<name>.md at repo root) or a path
    data: dict  # loader-specific data locations
    cache: str  # path to the verifier-score cache (JSON)
    results: str  # path to write the result table
    criteria: list = field(default_factory=list)  # criterion ids, in order
    n_evaluations: int = 8  # repeated verifications K per criterion
    pivots: int = 2  # number of pivots k in the tournament
    seed: int = 0  # seed for the random ring pass


BENCHMARKS = {
    "terminal_bench": Benchmark(
        name="TERMINAL-BENCH 2.0  (Capy · GPT-5.5, x5)",
        loader="terminal",
        prompts="terminal_bench",
        criteria=["specification", "output_match", "error_signals"],
        cache="cache/cache_terminal_capy_gpt-5.5.json",
        results="results/terminal_bench_capy.txt",
        data={"agent_dir": "data/terminal_bench_trajs/capy_gpt-5.5"},
    ),
    "swe_bench": Benchmark(
        name="SWE-BENCH VERIFIED  (mini-swe-agent, x3)",
        loader="swe",
        prompts="swe_bench",
        criteria=["root_cause", "code_review", "verification"],
        cache="cache/cache_swebench.json",
        results="results/swe_bench.txt",
        # runs: defaults to every run directory found under trajs_dir
        data={"trajs_dir": "data/swebench_verified_trajs"},
    ),
    "medagentbench": Benchmark(
        name="MEDAGENTBENCH  (Opus 4.8 max-effort, x5)",
        loader="med",
        prompts="medagentbench",
        criteria=["query", "consistency", "structure"],
        cache="cache/cache_medagentbench_opus48max.json",
        results="results/medagentbench.txt",
        data={
            "test_data": "data/medagentbench_trajs/problems.json",
            "output_dir": "data/medagentbench_trajs/trajs/opus-4.8-max",
            "run_names": ["run1", "run2", "run3", "run4", "run5"],
        },
    ),
}
