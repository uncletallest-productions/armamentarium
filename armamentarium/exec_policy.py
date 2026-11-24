"""exec_policy — decide whether a cognate may run an argv, and explain the answer.

Pure policy: parses ~/.config/armamentarium/exec.json, matches an argv against its rules,
and checks exec grants. It runs NOTHING. arma_exec (separate, later) calls ``decide()`` and only
executes on an ``allowed`` Decision -- which keeps this module exhaustively testable with no
side effects.

RULE FORMAT (exec.json, operator-owned; no agent root can write it)::

    {
      "version": 1,
      "roots": {
        "git-dom1": ["~/Datasets/Substrate", "~/Datasets/Cubicles/Scriptorum/Fabrica/*/*"],
        "disk":     {"patterns": ["/dev/disk/by-id/*"], "resolve": false}
      },
      "rules": {
        "git-status": {
          "mode": "always",
          "argv": ["git", "-C", "{path:git-dom1}", "status"],
          "options": {"-s": null, "-b": null, "--porcelain": null}
        },
        "journal": {
          "mode": "always", "sudo": true, "timeout": 20,
          "argv": ["journalctl", "--no-pager"],
          "options": {"-b": "{int?}", "-n": "{int}", "-u": "{unit}", "-o": "short-iso|json"}
        },
        "resolve-query": {"mode": "always", "argv": ["resolvectl", "query"], "args": ["{host}"]}
      }
    }

Matching, in order:
  1. ``argv`` -- the fixed prefix. Each element is a literal, an ``a|b|c`` alternation, or a
     typed placeholder ``{type}`` / ``{path:ROOT}``. Must match token for token.
  2. Then any mix of OPTIONS (tokens starting with ``-``) and POSITIONALS:
       * an option must be a key in ``options``. Its spec says what follows it:
           null        a bare flag, takes no value
           "{type}"    takes exactly one value (``--opt v`` or ``--opt=v``)
           "{type?}"   takes an optional value: the next token if it is of that type
           "a|b|c"     takes one of those literal values
         ANY option not listed is refused. That is the whole safety model for flags: there is
         no blacklist to forget to update (``git log --output``, ``git -c core.sshCommand=``,
         ``smartctl -t``, ``journalctl --vacuum-size`` all fail by construction).
       * positionals fill ``args`` in order; extra positionals are refused.
     Combined short flags (``-sb``) are NOT expanded: list them literally if wanted.
  3. ``--`` is refused (it would let a later token that looks like an option slip past as a
     positional).

Placeholder types are deliberately narrow and ASCII-only (no unicode look-alikes, no shell
metacharacters -- though argv is never handed to a shell anyway):
  int   -?digits (<=9)        name  [A-Za-z0-9._@:+-], not starting with -
  word  [A-Za-z0-9_-]         unit  systemd unit name (name chars + optional .suffix)
  host  hostname label/FQDN   time  journalctl-style time: [A-Za-z0-9 :._+-], <=40 chars
  ref   git revision (HEAD~1, a..b, HEAD@{1})   dataset  zfs pool/fs[@snap]
  text  free text <=4096, no control chars except tab/newline (commit messages)
  path:ROOT  realpath'd (unless the root says resolve:false), then must equal or sit under a
             path matching one of the root's glob patterns. Symlink escapes resolve OUT of the
             root and fail; ``..`` is normalised away before the check either way.

Per-rule fields: mode always|elevated (default elevated -- safe default), sudo (bool),
hosts (["*"] default), timeout (s, default 30), max_output (bytes, default 262144),
description (free text, shown in refusals and the future GUI).

Design + decisions: Substrate dom1/context/blocks/domain1-arma-exec.md.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EXEC_PATH = Path("~/.config/armamentarium/exec.json").expanduser()

TYPES = {
    "int": re.compile(r"^-?\d{1,9}$"),
    "name": re.compile(r"^[A-Za-z0-9._@:+][A-Za-z0-9._@:+-]{0,127}$"),
    "word": re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$"),
    "unit": re.compile(r"^[A-Za-z0-9@._:\\][A-Za-z0-9@._:\\-]{0,199}$"),
    "host": re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*$"),
    "time": re.compile(r"^[A-Za-z0-9 :._+-]{1,40}$"),
    # git revision: refs, HEAD~2, v1.0^, main..feature, HEAD@{1}. Never starts with '-'.
    "ref": re.compile(r"^[A-Za-z0-9._/@^~{}:+][A-Za-z0-9._/@^~{}:+-]{0,199}$"),
    # zfs/zpool dataset or snapshot: pool/a/b, pool/a@snap. Never starts with '-' or '/'.
    "dataset": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,254}$"),
    # free text (commit messages). Safe only because argv never meets a shell; still no
    # control chars beyond \t \n, and a leading '-' is impossible here anyway (it would be
    # parsed as an option first).
    "text": re.compile(r"^[^\x00-\x08\x0b-\x1f\x7f]{1,4096}$"),
}
PLACEHOLDER = re.compile(r"^\{(int|name|word|unit|host|time|text|ref|dataset)(\?)?\}$|^\{path:([A-Za-z0-9_-]+)\}$")
RULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RULE_KEYS = {"mode", "argv", "options", "args", "sudo", "hosts", "timeout", "max_output",
             "description"}
DEFAULTS = {"mode": "elevated", "sudo": False, "hosts": ["*"], "timeout": 30,
            "max_output": 256 * 1024}


class PolicyError(ValueError):
    """exec.json is missing, unreadable, or malformed. Fail closed: nothing is allowed."""


# --- loading + validation -------------------------------------------------------------------

@dataclass
class Root:
    name: str
    patterns: List[str]
    resolve: bool = True


@dataclass
class Rule:
    name: str
    argv: List[str]
    options: Dict[str, Optional[str]]
    args: List[str]
    mode: str
    sudo: bool
    hosts: List[str]
    timeout: int
    max_output: int
    description: str = ""

    def applies_to(self, host: str) -> bool:
        return "*" in self.hosts or host in self.hosts


@dataclass
class Policy:
    roots: Dict[str, Root]
    rules: Dict[str, Rule]
    source: str = ""


def _check_spec(spec: str, roots: Dict[str, dict], where: str) -> None:
    """A token spec is a literal, an a|b alternation, or a known placeholder."""
    if not isinstance(spec, str) or not spec:
        raise PolicyError(f"{where}: empty or non-string token spec {spec!r}")
    if spec.startswith("{"):
        m = PLACEHOLDER.match(spec)
        if not m:
            raise PolicyError(f"{where}: unknown placeholder {spec!r}")
        if m.group(3) and m.group(3) not in roots:
            raise PolicyError(f"{where}: {spec!r} names undefined root {m.group(3)!r}")


def load_policy(path: Optional[Path] = None) -> Policy:
    """Parse and validate exec.json. Any defect raises PolicyError (fail closed).

    Validation is strict on purpose: an unknown key is refused rather than ignored, so a typo
    like ``"opitons"`` cannot silently produce a rule with NO option restrictions... (it would
    produce one that allows no options at all, which is safe, but the operator meant something
    else and should be told).
    """
    p = Path(path) if path else EXEC_PATH
    try:
        st = p.stat()
    except FileNotFoundError:
        raise PolicyError(f"no exec policy at {p}: nothing may run until the operator writes one")
    except OSError as exc:
        raise PolicyError(f"exec policy {p} unreadable: {exc}") from exc
    if st.st_uid != os.geteuid():
        raise PolicyError(f"exec policy {p} is not owned by the user running the router; refusing")
    if st.st_mode & 0o022:
        raise PolicyError(f"exec policy {p} is group/world-writable (mode {st.st_mode & 0o777:o}); "
                          f"refusing. Fix: chmod 0644 {p}")
    try:
        data = json.loads(p.read_text())
    except FileNotFoundError:
        raise PolicyError(f"no exec policy at {p}: nothing may run until the operator writes one")
    except (OSError, ValueError) as exc:
        raise PolicyError(f"exec policy {p} unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PolicyError(f"{p}: expected an object with \"version\": 1")

    raw_roots = data.get("roots", {}) or {}
    if not isinstance(raw_roots, dict):
        raise PolicyError(f"{p}: 'roots' must be an object")
    roots: Dict[str, Root] = {}
    for name, spec in raw_roots.items():
        if isinstance(spec, list):
            spec = {"patterns": spec}
        if not isinstance(spec, dict) or not isinstance(spec.get("patterns"), list) \
                or not all(isinstance(x, str) and x for x in spec["patterns"]):
            raise PolicyError(f"root {name!r}: want a list of patterns or "
                              f"{{\"patterns\": [...], \"resolve\": bool}}")
        roots[name] = Root(name, [os.path.expanduser(x) for x in spec["patterns"]],
                           bool(spec.get("resolve", True)))

    raw_rules = data.get("rules")
    if not isinstance(raw_rules, dict) or not raw_rules:
        raise PolicyError(f"{p}: 'rules' must be a non-empty object")
    rules: Dict[str, Rule] = {}
    for name, r in raw_rules.items():
        where = f"rule {name!r}"
        if not RULE_NAME.match(name):
            raise PolicyError(f"{where}: bad rule name")
        if not isinstance(r, dict):
            raise PolicyError(f"{where}: must be an object")
        unknown = set(r) - RULE_KEYS
        if unknown:
            raise PolicyError(f"{where}: unknown key(s) {sorted(unknown)}")
        argv = r.get("argv")
        if not isinstance(argv, list) or not argv:
            raise PolicyError(f"{where}: 'argv' must be a non-empty list")
        if argv[0].startswith("{") or "|" in argv[0]:
            raise PolicyError(f"{where}: argv[0] must be a literal program name")
        for t in argv:
            _check_spec(t, raw_roots, where)
        options = r.get("options", {}) or {}
        if not isinstance(options, dict):
            raise PolicyError(f"{where}: 'options' must be an object")
        for opt, vspec in options.items():
            if not opt.startswith("-") or opt in ("-", "--") or "=" in opt:
                raise PolicyError(f"{where}: bad option name {opt!r}")
            if vspec is not None:
                _check_spec(vspec, raw_roots, f"{where} option {opt}")
        args = r.get("args", []) or []
        if not isinstance(args, list):
            raise PolicyError(f"{where}: 'args' must be a list")
        for t in args:
            _check_spec(t, raw_roots, f"{where} args")
        merged = {**DEFAULTS, **{k: v for k, v in r.items() if k in DEFAULTS}}
        if merged["mode"] not in ("always", "elevated"):
            raise PolicyError(f"{where}: mode must be always|elevated")
        if not isinstance(merged["hosts"], list) or not merged["hosts"]:
            raise PolicyError(f"{where}: hosts must be a non-empty list")
        rules[name] = Rule(name=name, argv=argv, options=options, args=args,
                           mode=merged["mode"], sudo=bool(merged["sudo"]),
                           hosts=[h.lower() for h in merged["hosts"]],
                           timeout=int(merged["timeout"]), max_output=int(merged["max_output"]),
                           description=str(r.get("description", "")))
    return Policy(roots=roots, rules=rules, source=str(p))


# --- matching ---------------------------------------------------------------------------------

class NoMatch(Exception):
    """One rule does not match. ``depth`` = how many argv tokens it accepted before failing,
    used to pick the NEAREST rule for the refusal message."""

    def __init__(self, depth: int, why: str):
        super().__init__(why)
        self.depth = depth
        self.why = why


def _under(path: str, root: Root) -> bool:
    """path equals, or sits under, a path matching one of the root's glob patterns."""
    p = path.rstrip("/") or "/"
    cands = [p]
    while p not in ("/", ""):
        p = os.path.dirname(p)
        cands.append(p)
    for pat in root.patterns:
        pat_n = pat.rstrip("/") or "/"
        if any(fnmatch.fnmatchcase(c, pat_n) for c in cands):
            return True
    return False


def _value_ok(spec: str, tok: str, policy: Policy) -> Tuple[bool, str]:
    """Does ``tok`` satisfy ``spec``? Returns (ok, reason-if-not)."""
    if not spec.startswith("{"):
        choices = spec.split("|")
        if tok in choices:
            return True, ""
        return False, (f"expected {' or '.join(repr(c) for c in choices)}"
                       if len(choices) > 1 else f"expected {spec!r}")
    m = PLACEHOLDER.match(spec)
    if m.group(1):
        if TYPES[m.group(1)].match(tok):
            return True, ""
        return False, f"{tok!r} is not a valid {m.group(1)}"
    root = policy.roots[m.group(3)]
    if not tok or "\x00" in tok:
        return False, "empty or NUL path"
    expanded = os.path.expanduser(tok)
    if not os.path.isabs(expanded):
        return False, f"path {tok!r} must be absolute (or ~/...)"
    resolved = os.path.realpath(expanded) if root.resolve else os.path.normpath(expanded)
    if _under(resolved, root):
        return True, ""
    shown = f"{tok!r}" + (f" (resolves to {resolved!r})" if resolved != expanded else "")
    return False, f"path {shown} is not under root {root.name!r} ({', '.join(root.patterns)})"


def _optional_type(spec: Optional[str]) -> bool:
    return bool(spec and spec.endswith("?}"))


def match_rule(rule: Rule, argv: List[str], policy: Policy) -> None:
    """Raise NoMatch if ``argv`` does not satisfy ``rule``; return None if it does."""
    if len(argv) < len(rule.argv):
        # report against the first missing token, depth = what did match
        for i, spec in enumerate(rule.argv[:len(argv)]):
            ok, why = _value_ok(spec.replace("?}", "}"), argv[i], policy)
            if not ok:
                raise NoMatch(i, f"token {i} {argv[i]!r}: {why}")
        raise NoMatch(len(argv), f"incomplete: rule needs {' '.join(rule.argv)}")
    for i, spec in enumerate(rule.argv):
        ok, why = _value_ok(spec, argv[i], policy)
        if not ok:
            raise NoMatch(i, f"token {i} {argv[i]!r}: {why}")

    i = len(rule.argv)
    pos = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            raise NoMatch(i, "'--' is not allowed")
        if tok.startswith("-") and len(tok) > 1 and not (
                pos < len(rule.args) and TYPES["int"].match(tok)
                and rule.args[pos] in ("{int}", "{int?}")):
            opt, eq, attached = tok.partition("=")
            if opt not in rule.options:
                allowed = ", ".join(sorted(rule.options)) or "none"
                raise NoMatch(i, f"option {opt!r} is not allowed by rule {rule.name!r} "
                                 f"(allowed: {allowed})")
            vspec = rule.options[opt]
            if vspec is None:
                if eq:
                    raise NoMatch(i, f"option {opt!r} takes no value")
                i += 1
                continue
            if eq:
                ok, why = _value_ok(vspec.replace("?}", "}"), attached, policy)
                if not ok:
                    raise NoMatch(i, f"value for {opt}: {why}")
                i += 1
                continue
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if _optional_type(vspec):
                if nxt is not None and _value_ok(vspec.replace("?}", "}"), nxt, policy)[0]:
                    i += 2
                else:
                    i += 1
                continue
            if nxt is None:
                raise NoMatch(i, f"option {opt!r} needs a value ({vspec})")
            ok, why = _value_ok(vspec, nxt, policy)
            if not ok:
                raise NoMatch(i + 1, f"value for {opt}: {why}")
            i += 2
            continue
        if pos >= len(rule.args):
            raise NoMatch(i, f"unexpected argument {tok!r} (rule {rule.name!r} takes "
                             f"{len(rule.args)} positional argument(s))")
        ok, why = _value_ok(rule.args[pos], tok, policy)
        if not ok:
            raise NoMatch(i, f"argument {pos + 1}: {why}")
        pos += 1
        i += 1
    missing = [a for a in rule.args[pos:] if not _optional_type(a)]
    if missing:
        raise NoMatch(i, f"missing argument(s): {' '.join(missing)}")


# --- grants + the decision ---------------------------------------------------------------------

@dataclass
class Decision:
    allowed: bool
    reason: str
    rule: Optional[str] = None
    mode: Optional[str] = None
    sudo: bool = False
    timeout: int = 30
    max_output: int = 256 * 1024
    grant: Optional[dict] = None
    hint: str = ""            # exact operator command that would make this allowed, if any

    def message(self) -> str:
        return self.reason + (f"\n  To allow: {self.hint}" if self.hint else "")


def _active_exec_grant(rule: str, host: str, grants: List[dict], now: float) -> Optional[dict]:
    for g in grants:
        if (g.get("kind") == "exec" and g.get("rule") == rule
                and str(g.get("host", "")).lower() == host
                and isinstance(g.get("expires_at"), (int, float)) and now < g["expires_at"]):
            return g
    return None


def decide(argv: List[str], host: str, policy: Policy, grants: List[dict],
           now: Optional[float] = None) -> Decision:
    """The one entry point: may ``argv`` run on ``host`` right now? Always explains why.

    A rule that matches but is elevated with no active grant is a REFUSAL with a hint -- never
    silently skipped in favour of a broader rule. If several rules match, an ``always`` rule wins
    over an ``elevated`` one (least privilege to run what was asked).
    """
    now = time.time() if now is None else now
    host = host.lower()
    if not argv or not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return Decision(False, "refused: argv must be a non-empty list of strings")
    if any("\x00" in a for a in argv):
        return Decision(False, "refused: NUL byte in argv")

    matched: List[Rule] = []
    nearest: Optional[Tuple[int, Rule, str]] = None
    wrong_host: List[Rule] = []
    for rule in policy.rules.values():
        if rule.argv[0] != argv[0]:
            continue
        try:
            match_rule(rule, argv, policy)
        except NoMatch as nm:
            if nearest is None or nm.depth > nearest[0]:
                nearest = (nm.depth, rule, nm.why)
            continue
        (matched if rule.applies_to(host) else wrong_host).append(rule)

    shown = " ".join(argv)
    if not matched:
        if wrong_host:
            r = wrong_host[0]
            return Decision(False, f"refused: `{shown}` matches rule {r.name!r}, which does not "
                                   f"apply on {host} (hosts: {', '.join(r.hosts)})", rule=r.name)
        if nearest:
            _, r, why = nearest
            return Decision(False, f"refused: `{shown}` matches no rule. Nearest is {r.name!r}: "
                                   f"{why}", rule=r.name)
        return Decision(False, f"refused: no rule runs {argv[0]!r} at all")

    matched.sort(key=lambda r: r.mode != "always")
    for rule in matched:
        if rule.mode == "always":
            return Decision(True, f"allowed by {rule.name!r} (always)", rule=rule.name,
                            mode="always", sudo=rule.sudo, timeout=rule.timeout,
                            max_output=rule.max_output)
        g = _active_exec_grant(rule.name, host, grants, now)
        if g:
            left = int(g["expires_at"] - now)
            return Decision(True, f"allowed by {rule.name!r} (elevated; grant has {left}s left)",
                            rule=rule.name, mode="elevated", sudo=rule.sudo,
                            timeout=rule.timeout, max_output=rule.max_output, grant=g)
    r = matched[0]
    host_arg = "" if host == _this_host() else f" {host}"
    return Decision(False, f"refused: `{shown}` needs rule {r.name!r}, which requires the "
                           f"operator's approval and there is no active grant for it on {host}",
                    rule=r.name, mode="elevated",
                    hint=f"arma-elevate exec {r.name}{host_arg} [ttl]")


def _this_host() -> str:
    import socket
    return socket.gethostname().split(".")[0].lower()
