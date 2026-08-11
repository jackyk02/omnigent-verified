"""
Fine-grained reward model.

Rather than collapsing a verifier's judgement into one discrete label (as in
LLM-as-a-Judge), LLM-as-a-Verifier reads the model's probability distribution
over an ordered set of score tokens and takes its expectation. The reward of a
trajectory tau on task t is

    R(t, tau) = (1 / C K) * sum_c sum_k sum_g  p_theta(v_g | t, c, tau) * phi(v_g)

  C  = number of evaluation criteria
  K  = number of repeated verifications
  G  = number of ordered score tokens (granularity)
  v_g = the g-th score token,  phi(v_g) = its scalar value
  p_theta = probability the verifier assigns to token v_g (from logprobs)

This module provides the granularity-20 scale, the Gemini logprob client, the
score-token expectation `extract_score`, the pairwise prompt, and a cached
batch scorer that only scores the pairs a pivot tournament actually needs.
"""

import base64
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

DEFAULT_MODEL = "gemini-2.5-flash"


class MissingAPIKeyError(RuntimeError):
    """No verifier backend found in the environment.

    Set ``OPENAI_BASE_URL`` to use an OpenAI-compatible server (e.g. vLLM or
    SGLang), or ``VERTEX_API_KEY`` to use Gemini via Vertex AI (in the
    environment or a ``.env`` file in the working directory), or pass a
    pre-built client via the ``client=`` argument. Either way the backend
    must expose token-level logprobs, which the fine-grained reward needs.
    """


# ---------------------------------------------------------------------------
# g = 20 score-token scale
# ---------------------------------------------------------------------------

GRANULARITY = 20

SCALE = {
    "scale_description": (
        "Rate how likely the agent correctly solved the task on a "
        "20-point scale using letters A through T:\n"
        "  A = clearly and completely succeeded with verified output (best)\n"
        "  B-D = succeeded with only minor issues\n"
        "  E-G = above average, mostly correct with some issues\n"
        "  H-J = uncertain, leans toward success\n"
        "  K-M = uncertain, leans toward failure\n"
        "  N-P = below average, significant issues remain\n"
        "  Q-S = failed with some partial progress\n"
        "  T = clearly and completely failed (worst)"
    ),
    "score_format": "LETTER_A_TO_T",
    "valid_tokens": {
        **{chr(65 + i): float(GRANULARITY - i) for i in range(GRANULARITY)},
        **{chr(97 + i): float(GRANULARITY - i) for i in range(GRANULARITY)},
    },
}


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------


def load_dotenv(root_dir=None):
    """Load KEY=value pairs from `<root_dir>/.env` (default: the working
    directory) into the environment, without overriding existing values."""
    env_path = os.path.join(root_dir or os.getcwd(), ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def create_gemini_client():
    """Build a ``google-genai`` client from ``VERTEX_API_KEY`` (a ``.env``
    file in the working directory is loaded first). Only Vertex AI is
    supported — extracting the score-token logprob distribution requires the
    token-level logprobs the Vertex API exposes. Raises `MissingAPIKeyError`
    if the key is not set."""
    from google import genai

    load_dotenv()
    vertex_key = os.environ.get("VERTEX_API_KEY")
    if vertex_key:
        return genai.Client(vertexai=True, api_key=vertex_key)
    raise MissingAPIKeyError(
        "set VERTEX_API_KEY in .env or environment (Vertex AI only — "
        "logprob extraction needs the Vertex API)"
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible client (vLLM, SGLang, OpenAI, ...)
# ---------------------------------------------------------------------------


def create_openai_client(base_url=None, api_key=None):
    """Build an ``openai`` client for any OpenAI-compatible server that
    returns token-level logprobs (vLLM ``vllm serve``, SGLang, OpenAI).
    ``base_url`` defaults to ``OPENAI_BASE_URL`` (e.g.
    ``http://localhost:8000/v1`` for a local vLLM server) and ``api_key`` to
    ``OPENAI_API_KEY`` (any string works for a local vLLM server)."""
    from openai import OpenAI

    load_dotenv()
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    if not base_url:
        raise MissingAPIKeyError(
            "set OPENAI_BASE_URL to an OpenAI-compatible endpoint "
            "(e.g. http://localhost:8000/v1 for vLLM)"
        )
    return OpenAI(base_url=base_url, api_key=api_key or os.environ.get("OPENAI_API_KEY", "EMPTY"))


def create_client():
    """Build a verifier client from the environment: an OpenAI-compatible
    client when ``OPENAI_BASE_URL`` is set (vLLM / SGLang / OpenAI),
    otherwise a Gemini client from ``VERTEX_API_KEY``. Raises
    `MissingAPIKeyError` when neither is configured."""
    load_dotenv()
    if os.environ.get("OPENAI_BASE_URL"):
        return create_openai_client()
    return create_gemini_client()


def _is_openai_client(client):
    """OpenAI clients expose `.chat.completions`; google-genai clients
    don't have `.chat`."""
    return hasattr(client, "chat")


# ---------------------------------------------------------------------------
# Image inputs — every public API accepts `images` as one image or a list;
# each image is a local file path, an http(s) URL, or raw bytes. Images are
# attached to the verifier message after the text prompt, in order.
# ---------------------------------------------------------------------------


def as_image_list(images):
    """Normalize the `images` argument: None -> [], a single image (path /
    URL / bytes) -> [image], a sequence -> list."""
    if images is None:
        return []
    if isinstance(images, (str, bytes, os.PathLike)):
        return [images]
    return list(images)


def _sniff_mime(data):
    """Image MIME type from magic bytes (PNG/JPEG/GIF/WebP; PNG default)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def load_image(image):
    """Load one image into (bytes, mime_type). Accepts a local file path,
    an http(s) URL, or raw image bytes."""
    if isinstance(image, bytes):
        data = image
    elif isinstance(image, (str, os.PathLike)):
        s = os.fspath(image)
        if s.startswith(("http://", "https://")):
            from urllib.request import urlopen

            with urlopen(s) as r:
                data = r.read()
        else:
            with open(s, "rb") as f:
                data = f.read()
    else:
        raise TypeError(
            f"unsupported image type {type(image).__name__}: expected a "
            "file path, an http(s) URL, or raw bytes"
        )
    return data, _sniff_mime(data)


def resolve_model(client, model=DEFAULT_MODEL):
    """The model name to send to `client`. For an OpenAI-compatible client
    still pointed at the Gemini default, ask the server what it serves (a
    vLLM instance serves exactly one model) and cache the answer, so
    ``select(...)`` works against a local server without a ``model=``
    argument."""
    if not _is_openai_client(client) or model != DEFAULT_MODEL:
        return model
    served = getattr(client, "_llm_verifier_model", None)
    if served is None:
        served = client.models.list().data[0].id
        client._llm_verifier_model = served
    return served


def call_openai(client, prompt, model=DEFAULT_MODEL, top_logprobs=20, images=None):
    """Call an OpenAI-compatible verifier with logprobs.
    Returns (text, tokens, position_logprobs) — the same shape as
    `call_gemini`, so `extract_score` works unchanged."""
    content = prompt
    imgs = as_image_list(images)
    if imgs:
        content = [{"type": "text", "text": prompt}]
        for img in imgs:
            data, mime = load_image(img)
            b64 = base64.b64encode(data).decode("ascii")
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
    params = dict(
        model=resolve_model(client, model),
        messages=[{"role": "user", "content": content}],
        max_tokens=4096,
        temperature=1.0,
        logprobs=True,
        top_logprobs=top_logprobs,  # the OpenAI API caps this at 20
    )
    try:
        # vLLM/SGLang only: skip hybrid-thinking so score tags come fast.
        response = client.chat.completions.create(
            extra_body={"chat_template_kwargs": {"enable_thinking": False}}, **params
        )
    except Exception:
        response = client.chat.completions.create(**params)

    choice = response.choices[0]
    text = choice.message.content or ""
    tokens = None
    position_logprobs = None

    if choice.logprobs and choice.logprobs.content:
        tokens, position_logprobs = [], []
        for pos in choice.logprobs.content:
            tokens.append(pos.token)
            alts = [(alt.token, alt.logprob) for alt in (pos.top_logprobs or [])]
            if not alts:
                alts = [(pos.token, pos.logprob)]
            position_logprobs.append(alts)

    # Open models don't reliably emit the requested score tags (or fill
    # them with digits instead of scale letters). Instead of trusting the
    # sampled tags, keep only the analysis, then prefill each tag and read
    # the letter distribution at exactly that position.
    tags = [t for t in ("<score_A>", "<score_B>") if t in prompt]
    if tags:
        idx = min([text.find(t) for t in tags if t in (text or "")] or [len(text or "")])
        analysis = (text or "")[:idx].rstrip()
        text, tokens, position_logprobs = _score_tags_by_prefill(
            client, params["model"], params["messages"], analysis, tags, top_logprobs
        )

    return text, tokens, position_logprobs


def _score_tags_by_prefill(client, model, messages, text, tags, top_logprobs=20):
    """Read the `<score_X>` letter distributions by prefill. For each tag,
    continue the assistant message with the analysis plus the tag prefilled
    (vLLM/SGLang ``continue_final_message``), constrained to the 20 scale
    letters via structured outputs where supported, and read the verifier's
    renormalized letter distribution at exactly that position. Returns
    (text, tokens, position_logprobs) in the `call_gemini` shape; a server
    without prefill support returns them tag-less (scores fall back to
    0.5)."""
    tokens = []
    position_logprobs = []
    for tag in tags:
        prefix = (text or "") + f"\n{tag}"
        # Some models (e.g. Qwen VL) put nearly all their mass on the
        # letter WITH a leading space after the prefilled ">"; allow both
        # spellings so the grammar mask keeps the real distribution.
        letters = [chr(65 + i) for i in range(GRANULARITY)]
        letters += [" " + c for c in letters]
        try:
            # Constrain the prefilled position to the 20 score letters, so
            # the returned top-logprobs are the renormalized distribution
            # over the scale itself.
            response = client.chat.completions.create(
                model=model,
                messages=messages + [{"role": "assistant", "content": prefix}],
                max_tokens=1,
                temperature=1.0,
                logprobs=True,
                top_logprobs=top_logprobs,
                extra_body={
                    "add_generation_prompt": False,
                    "continue_final_message": True,
                    "structured_outputs": {"choice": letters},
                },
            )
        except Exception:
            return text, tokens or None, position_logprobs or None
        choice = response.choices[0]
        letter = (choice.message.content or "").strip()
        alts = []
        if choice.logprobs and choice.logprobs.content:
            pos = choice.logprobs.content[0]
            alts = [(alt.token, alt.logprob) for alt in (pos.top_logprobs or [])]
        closing = "</" + tag[1:]
        text = prefix + letter + closing
        tokens += [f"\n{tag}", letter, closing]
        position_logprobs += [[(f"\n{tag}", 0.0)], alts, [(closing, 0.0)]]
    return text, tokens or None, position_logprobs or None


def call_verifier(client, prompt, model=DEFAULT_MODEL, top_logprobs=20, images=None):
    """Backend dispatch: route to the OpenAI-compatible or Gemini call based
    on the client type. Returns (text, tokens, position_logprobs)."""
    if _is_openai_client(client):
        return call_openai(client, prompt, model, top_logprobs, images=images)
    return call_gemini(client, prompt, model, top_logprobs, images=images)


def call_gemini(client, prompt, model=DEFAULT_MODEL, top_logprobs=20, images=None):
    """Call the verifier model with logprobs.
    Returns (text, tokens, position_logprobs)."""
    from google.genai.types import Content, GenerateContentConfig, Part, ThinkingConfig

    config = GenerateContentConfig(
        max_output_tokens=4096,
        temperature=1.0,
        response_logprobs=True,
        logprobs=top_logprobs,
        thinking_config=ThinkingConfig(thinking_budget=0),
    )

    parts = [Part(text=prompt)]
    for img in as_image_list(images):
        data, mime = load_image(img)
        parts.append(Part.from_bytes(data=data, mime_type=mime))

    response = client.models.generate_content(
        model=model,
        contents=[Content(role="user", parts=parts)],
        config=config,
    )

    text = response.text or ""
    tokens = None
    position_logprobs = None

    candidate = response.candidates[0]
    if candidate.logprobs_result and candidate.logprobs_result.top_candidates:
        position_logprobs = []
        for pos in candidate.logprobs_result.top_candidates:
            alts = [(lp.token, lp.log_probability) for lp in pos.candidates]
            position_logprobs.append(alts)
        if candidate.logprobs_result.chosen_candidates:
            tokens = [c.token for c in candidate.logprobs_result.chosen_candidates]

    return text, tokens, position_logprobs


# ---------------------------------------------------------------------------
# Score-token expectation:  sum_g p(v_g) * phi(v_g), normalized to [0, 1]
# ---------------------------------------------------------------------------


def _find_tag_logprobs(tokens, position_logprobs, tag):
    if not tokens or not position_logprobs:
        return None
    text_so_far = ""
    for i, tok in enumerate(tokens):
        text_so_far += tok
        if text_so_far.rstrip().endswith(tag):
            if i + 1 < len(position_logprobs):
                return position_logprobs[i + 1]
    return None


def extract_score(text, tokens, position_logprobs, tag):
    """Expected score over the verifier's token distribution at `tag`,
    normalized to [0, 1]. Falls back to parsing the literal text token."""
    valid_tokens = SCALE["valid_tokens"]

    tag_lp = _find_tag_logprobs(tokens, position_logprobs, tag)
    probs = {}
    if tag_lp:
        for tok_str, logprob in tag_lp:
            tok = tok_str.strip()
            if tok in valid_tokens:
                val = valid_tokens[tok]
                p = math.exp(logprob)
                probs[val] = max(probs.get(val, 0.0), p)

    if probs:
        unique_vals = sorted(set(valid_tokens.values()))
        min_val, max_val = min(unique_vals), max(unique_vals)
        total_p = sum(probs.values())
        expected = sum(v * p for v, p in probs.items()) / total_p
        return (expected - min_val) / (max_val - min_val) if max_val > min_val else 0.5

    tag_name = tag.strip("<>")
    pattern = rf"<{re.escape(tag_name)}>\s*(.+?)\s*</{re.escape(tag_name)}>"
    match = re.search(pattern, text or "", re.IGNORECASE)
    if match:
        tok = match.group(1).strip()
        raw_val = valid_tokens.get(tok)
        if raw_val is None:
            for vt, val in valid_tokens.items():
                if tok.lower() == vt.lower():
                    raw_val = val
                    break
        if raw_val is not None:
            unique_vals = sorted(set(valid_tokens.values()))
            min_val, max_val = min(unique_vals), max(unique_vals)
            return (raw_val - min_val) / (max_val - min_val) if max_val > min_val else 0.5

    return 0.5


# ---------------------------------------------------------------------------
# Pairwise prompt + single-criterion scoring
# ---------------------------------------------------------------------------


def build_prompt(problem, trace_a, trace_b, criterion, ground_truth_note, n_images=0):
    """One pairwise prompt focused on a single evaluation criterion."""
    images_note = (
        f"**Attached images:** {n_images} image(s) are attached to this "
        "message, in order; they are part of the task context.\n\n"
        if n_images
        else ""
    )
    return (
        "You are an expert evaluator of AI coding agents. "
        "You will see a task description and two agent trajectories. "
        f"Your job is to evaluate them on ONE specific criterion: "
        f"**{criterion['name']}**.\n\n"
        f"{ground_truth_note}\n\n"
        f"**Task:**\n{problem}\n\n"
        f"{images_note}"
        f"**Trajectory A:**\n{trace_a}\n\n"
        f"**Trajectory B:**\n{trace_b}\n\n"
        f"**Evaluation Guideline — {criterion['name']}:**\n"
        f"{criterion['description']}\n\n"
        f"Score each trajectory ONLY on this specific criterion. Ignore other "
        f"aspects of the trajectory that are not relevant to "
        f'"{criterion["name"]}".\n\n'
        f"**Rating Scale:**\n{SCALE['scale_description']}\n\n"
        "Then output your final scores:\n"
        f"<score_A>{SCALE['score_format']}</score_A>\n"
        f"<score_B>{SCALE['score_format']}</score_B>\n\n"
        "Begin your analysis now."
    )


def score_pair_criterion(
    client,
    problem,
    trace_a,
    trace_b,
    criterion,
    ground_truth_note,
    model=DEFAULT_MODEL,
    images=None,
):
    """Score (A, B) for a single criterion, returning fine-grained
    rewards (R_A, R_B) in [0, 1]. `images` (one image or a list) is attached
    to the verifier message as task context."""
    imgs = as_image_list(images)
    prompt = build_prompt(
        problem, trace_a, trace_b, criterion, ground_truth_note, n_images=len(imgs)
    )
    text, tokens, position_logprobs = call_verifier(client, prompt, model, images=imgs)
    ra = extract_score(text, tokens, position_logprobs, "<score_A>")
    rb = extract_score(text, tokens, position_logprobs, "<score_B>")
    return ra, rb


# ---------------------------------------------------------------------------
# Directed cache key + the per-comparison reward used by the tournament
# ---------------------------------------------------------------------------
#
# Comparisons are *directed*: candidate `a` is shown in slot A and `b` in slot
# B of the verifier prompt. PPT's ring pass relies on this — each candidate is
# placed in A exactly once and in B exactly once around the cycle, so the
# verifier's slot bias cancels. The cache key therefore records the ordered
# pair (a, b); (a, b) and (b, a) are distinct entries.


def cache_key(crit_id, task_name, a, b, rep):
    return f"{crit_id}|{task_name}|{a},{b}|{rep}"


def directed_reward(scores, task_name, a, b, criteria_ids, n_reps):
    """Fine-grained rewards (R_a, R_b) for the directed comparison (a in slot
    A, b in slot B), averaged over all criteria and repeated verifications.
    Missing entries default to a tie (0.5)."""
    if a == b:
        return 0.5, 0.5
    sa = sb = 0.0
    cnt = 0
    for cid in criteria_ids:
        for rep in range(n_reps):
            entry = scores.get(cache_key(cid, task_name, a, b, rep), {})
            sa += entry.get("score_A", 0.5)
            sb += entry.get("score_B", 0.5)
            cnt += 1
    return (sa / cnt, sb / cnt) if cnt else (0.5, 0.5)


class LazyClient:
    """Create the verifier client on first use (OpenAI-compatible when
    ``OPENAI_BASE_URL`` is set, Gemini otherwise), so reproducing a fully
    cached run never needs an API key."""

    def __init__(self):
        self._client = None

    def get(self):
        if self._client is None:
            self._client = create_client()
        return self._client


# ---------------------------------------------------------------------------
# Cached batch scoring — only the directed pairs PPT actually needs
# ---------------------------------------------------------------------------


def _progress_iter(futures, progress):
    """Wrap `as_completed(futures)` in tqdm when progress is on and tqdm is
    available; fall back to plain iteration otherwise."""
    it = as_completed(futures)
    if not progress:
        return it, lambda **kw: None
    try:
        from tqdm import tqdm
    except ImportError:
        return it, lambda **kw: None
    pbar = tqdm(it, total=len(futures), desc="Scoring")
    return pbar, pbar.set_postfix


def score_directed_pairs(
    lazy_client,
    tasks,
    needed_pairs,
    criteria,
    ground_truth_note,
    n_reps,
    max_workers,
    cache_file,
    model=DEFAULT_MODEL,
    progress=True,
    on_error="tie",
):
    """Score every (criterion, rep) for the requested directed (task, a, b)
    pairs and merge into the cache on disk.

    `needed_pairs` maps task_name -> iterable of directed (a, b) comparisons.
    Only comparisons missing from the cache trigger API calls, so the cost of a
    run scales with the PPT comparison count, not C(N, 2).

    `on_error` controls failed verifier calls: ``"tie"`` scores the comparison
    0.5/0.5 for this run only (failures are **never written to the cache**, so
    a transient API error can't become a permanent fake tie), ``"raise"``
    re-raises the first failure. Returns the merged scores dict."""
    if on_error not in ("tie", "raise"):
        raise ValueError(f"on_error must be 'tie' or 'raise', got {on_error!r}")

    cached = {}
    if cache_file and os.path.exists(cache_file):
        with open(cache_file) as f:
            cached = json.load(f)

    jobs = []
    for task_name, pairs in needed_pairs.items():
        trials = tasks[task_name]
        for a, b in pairs:
            for crit in criteria:
                for rep in range(n_reps):
                    key = cache_key(crit["id"], task_name, a, b, rep)
                    if key not in cached:
                        jobs.append(
                            (
                                key,
                                trials[a]["problem"],
                                trials[a]["trace"],
                                trials[b]["trace"],
                                crit,
                                trials[a].get("images"),
                            )
                        )

    log = print if progress else (lambda *a, **kw: None)

    if not jobs:
        log(f"  All scores cached ({len(cached)} entries)")
        return cached

    log(f"  {len(jobs)} scoring jobs ({len(cached)} cached)")

    client = lazy_client.get()
    # `results` is what this run sees; `cached` is what gets persisted.
    # Error ties go into `results` only.
    results = dict(cached)
    errors = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                score_pair_criterion, client, prob, ta, tb, crit, ground_truth_note, model, images
            ): key
            for key, prob, ta, tb, crit, images in jobs
        }
        iterator, set_postfix = _progress_iter(futures, progress)
        save_every = max(1, len(futures) // 20)
        done = 0

        for future in iterator:
            key = futures[future]
            try:
                ra, rb = future.result()
                entry = {"score_A": ra, "score_B": rb}
                cached[key] = entry
                results[key] = entry
            except Exception as e:
                if on_error == "raise":
                    for f in futures:
                        f.cancel()
                    raise
                errors += 1
                results[key] = {"score_A": 0.5, "score_B": 0.5}
                if errors <= 3:
                    log(f"\n  Error: {e}")
            done += 1
            set_postfix(errors=errors)
            if cache_file and done % save_every == 0:
                with open(cache_file, "w") as f:
                    json.dump(cached, f)

    if cache_file:
        with open(cache_file, "w") as f:
            json.dump(cached, f)

    log(f"  Done ({errors} errors)")
    return results
