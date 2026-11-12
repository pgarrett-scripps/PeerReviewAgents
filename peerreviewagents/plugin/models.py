"""Which model runs in which slot, and the refusal when one is not available.

Stage A ran three core reviewers on one model and they caught no more than one
of them did: three readings of the same manuscript by the same reader. So the
core reviewers are deliberately different models, and this module is the only
place that decides which:

* **Core reviewers** take their model from an ordered list and reviewer *i*
  uses ``list[(i - 1) % len(list)]``. The default list is
  ``claude-sonnet-5-5, claude-opus-5-5, claude-haiku-5-5``, so one reviewer is
  Sonnet, two are Sonnet and Opus, three add Haiku, and a fourth starts the
  list again. ``revision-1`` is ``core-1`` revising its own findings, so it
  takes ``core-1``'s model and never a different one.
* **Specialists** are one model, ``claude-sonnet-5-5``.
* **The editor** is ``claude-fable-5-2``, and **the recorder**
  ``claude-haiku-5-5``.

Every one of them is configurable, by flag on ``peerreview review``, by
environment variable in a session the starter launched, and by argument to
``ingest`` for a session that runs the workflow itself. There is no billing or
usage gate anywhere in here: which models a login may run is the user's
business, and this module's only job is to be honest about what it asked for.

**A model that is not available is a refusal, not a substitution.** Claude Code
itself falls back silently — a blocked family alias runs the newest permitted
version of that family and any other blocked value runs the inherited model —
so a run that asked for Opus and got Sonnet would be recorded as the arm it was
not. :func:`refuse` fails at startup instead, in one line per slot that names
the slot, the model and the flag that changes it.

Availability is read from the ``availableModels`` allowlist in the settings
files Claude Code reads, concatenated across those files the way Claude Code
concatenates them. A name outside :data:`KNOWN` is refused too, but for the
reason it is actually refused for: this version of the plugin does not know
that model, not that the login may not run it.

Two other things here are about the gap between the plan and what runs.
:func:`settle` is the one merge of flags over environment, so the starter and
the server cannot disagree about which models a run asked for. And
:func:`substitutions` compares what each slot was assigned against what it
reported running, because Claude Code's silent fallback is exactly what
:func:`refuse` cannot catch when the substitution happens inside the session:
a different family is a run that did not happen as recorded, and a different
version of the same family is simply the version that ran.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

#: The family aliases Claude Code accepts, and the model each one means here.
ALIASES = {
    'sonnet': 'claude-sonnet-5-5',
    'opus': 'claude-opus-5-5',
    'haiku': 'claude-haiku-5-5',
    'fable': 'claude-fable-5-2',
}
#: The alias for one full model id, which is what a per-spawn override takes.
FAMILY = {full: alias for alias, full in ALIASES.items()}
#: Every model this plugin knows how to ask for by its full name.
KNOWN = tuple(sorted(ALIASES.values()))

DEFAULT_REVIEWER_MODELS = ('claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5')
DEFAULT_SPECIALIST_MODEL = 'claude-sonnet-5-5'
DEFAULT_EDITOR_MODEL = 'claude-fable-5-2'
DEFAULT_RECORDER_MODEL = 'claude-haiku-5-5'

#: The flag that changes each role's model, named in the refusal.
FLAG = {
    'core': '--reviewer-models',
    'specialist': '--specialist-model',
    'editor': '--editor-model',
    'recorder': '--recorder-model',
}
#: How the starter hands the choice to the session it launches.
ENVIRONMENT = {
    'core': 'PEERREVIEW_REVIEWER_MODELS',
    'specialist': 'PEERREVIEW_SPECIALIST_MODEL',
    'editor': 'PEERREVIEW_EDITOR_MODEL',
    'recorder': 'PEERREVIEW_RECORDER_MODEL',
}
#: Where Claude Code reads ``availableModels`` from, highest precedence first.
MANAGED_SETTINGS = (
    '/etc/claude-code/managed-settings.json',
    '/Library/Application Support/ClaudeCode/managed-settings.json',
    'C:\\ProgramData\\ClaudeCode\\managed-settings.json',
)
#: The variable that sets one model for every subagent, whatever a slot asked.
SUBAGENT_MODEL = 'CLAUDE_CODE_SUBAGENT_MODEL'
#: The Claude Code release from which that variable ranks *below* a per-spawn
#: override and an agent file rather than above both.
SUBAGENT_PRECEDENCE_FIXED = (2, 1, 251)


def normalize(name):
    """One model name as its full id: an alias is resolved, anything else kept.

    A name this module does not know is returned unchanged rather than guessed
    at, so :func:`problems` can refuse it by the name the caller actually typed.
    """
    text = str(name or '').strip()
    return ALIASES.get(text.casefold(), text)


def spawn_name(name):
    """What to pass as a per-spawn model override for ``name``.

    A workflow script's per-invocation override is documented to take a family
    alias, so a full id is sent as its own family's alias and the full id is
    what gets recorded. The two differ only when a family's newest permitted
    version is not the version pinned here, and that is the one case where the
    model an agent reports having run as is worth more than the model this
    module asked for — which is why both are kept in the run files.
    """
    return FAMILY.get(normalize(name), normalize(name))


def parse_list(text):
    """``"sonnet, opus,haiku"`` as the ordered tuple of full model ids.

    An empty string means "unset", not "an empty list": the caller then keeps
    the default. An *empty entry* inside a list that is otherwise a choice —
    ``sonnet,,haiku`` — is refused rather than dropped, because dropping it
    silently changes which reviewer runs which model: with the empty item
    dropped, reviewer 2 runs Haiku instead of the model the typo meant to name,
    and nothing in the run would say so.

    A list may be separated by commas or, when it holds no comma at all, by
    spaces. Whitespace around a comma is not an entry.
    """
    if text is None:
        return ()
    if isinstance(text, (list, tuple)):
        parts = [str(part).strip() for part in text]
    else:
        text = str(text).strip()
        if not text:
            return ()
        parts = ([part.strip() for part in text.split(',')] if ',' in text else text.split())
    if any(not part for part in parts):
        raise ValueError(f'a model list has an empty entry: {text!r}; separate the models with '
                         'single commas, as in sonnet,opus,haiku (no model was run)')
    return tuple(normalize(part) for part in parts)


def choice(wanted, default):
    """``wanted`` when the caller gave one, else ``default``, always normalized."""
    text = str(wanted or '').strip()
    return normalize(text) if text else normalize(default)


def from_environment(environ=None):
    """The three settings as the starter passes them to a session, or ``{}``.

    A variable that is not set, or is set to nothing, is absent: a blank value
    is how a shell passes "I did not choose", and it must not clear the default.
    """
    environ = os.environ if environ is None else environ
    chosen = {}
    reviewers = parse_list(environ.get(ENVIRONMENT['core']))
    if reviewers:
        chosen['reviewers'] = list(reviewers)
    for key, role in (('specialist', 'specialist'), ('editor', 'editor'),
                      ('recorder', 'recorder')):
        value = str(environ.get(ENVIRONMENT[role]) or '').strip()
        if value:
            chosen[key] = normalize(value)
    return chosen


def settle(reviewers=None, specialist=None, editor=None, recorder=None, environ=None):
    """What a run asked for: the arguments it was given, over the environment.

    This is the only merge of the two, so the process that launches a session
    and the server inside it cannot reach different answers. The starter used
    to plan from its flags alone while passing the environment through to the
    session, which meant that with ``PEERREVIEW_REVIEWER_MODELS`` set and no
    flag given, the starter wrote the defaults into the folder and the server
    then refused that folder for holding a run under other models.

    An argument beats a variable, because a flag is this run's own choice and
    a variable is the one the environment was left in. What nobody chose is
    absent from the result, so :func:`assign` applies the defaults once.
    """
    chosen = from_environment(environ)
    listed = parse_list(reviewers)
    if listed:
        chosen['reviewers'] = list(listed)
    for value, key in ((specialist, 'specialist'), (editor, 'editor'), (recorder, 'recorder')):
        text = str(value or '').strip()
        if text:
            chosen[key] = normalize(text)
    return chosen


def to_environment(reviewers=None, specialist=None, editor=None, recorder=None):
    """The variables that carry a choice into a launched session, or ``{}``.

    Only what the caller actually chose is passed on: an unset variable leaves
    the session on the defaults, and a variable set to a model nobody asked for
    would be a choice this process invented.
    """
    chosen = {}
    listed = parse_list(reviewers)
    if listed:
        chosen[ENVIRONMENT['core']] = ','.join(listed)
    for value, role in ((specialist, 'specialist'), (editor, 'editor'), (recorder, 'recorder')):
        text = str(value or '').strip()
        if text:
            chosen[ENVIRONMENT[role]] = normalize(text)
    return chosen


def role_of(slot):
    """The role one slot's model comes from, by the slot names the server mints."""
    if slot == 'editor':
        return 'editor'
    if slot == 'recorder':
        return 'recorder'
    if str(slot).startswith('spec-'):
        return 'specialist'
    return 'core'


def _index(slot):
    """Which core reviewer a slot is, 1-based. ``revision-1`` is ``core-1``."""
    if slot == 'revision-1':
        return 1
    try:
        return int(str(slot).rsplit('-', 1)[1])
    except (IndexError, ValueError):
        return 1


def assign(slots, reviewers=None, specialist=None, editor=None, recorder=None):
    """The model for every slot in ``slots``, as an ordered ``{slot: model}``.

    ``reviewers`` is the ordered list; the rest are one model each. Anything
    left out takes this module's default. The mapping is the run's record of
    what was asked for, so it is computed once, at ingest, and written down.
    """
    chosen = tuple(parse_list(reviewers)) or tuple(
        normalize(model) for model in DEFAULT_REVIEWER_MODELS)
    specialist = choice(specialist, DEFAULT_SPECIALIST_MODEL)
    editor = choice(editor, DEFAULT_EDITOR_MODEL)
    recorder = choice(recorder, DEFAULT_RECORDER_MODEL)
    out = {}
    for slot in slots:
        role = role_of(slot)
        if role == 'core':
            out[slot] = chosen[(_index(slot) - 1) % len(chosen)]
        elif role == 'specialist':
            out[slot] = specialist
        elif role == 'editor':
            out[slot] = editor
        else:
            out[slot] = recorder
    return out


def _settings_models(path):
    """``availableModels`` from one settings file, or ``None`` if it says nothing."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None
    try:
        settings = json.loads(text)
    except ValueError:
        return None
    if not isinstance(settings, dict):
        return None
    listed = settings.get('availableModels')
    if isinstance(listed, str):
        listed = [listed]
    if not isinstance(listed, list):
        return None
    names = [str(name).strip() for name in listed if str(name or '').strip()]
    return names or None


def settings_files(environ=None, project=None):
    """The settings files Claude Code reads, highest precedence first."""
    environ = os.environ if environ is None else environ
    paths = [Path(name) for name in MANAGED_SETTINGS]
    config = str(environ.get('CLAUDE_CONFIG_DIR') or '').strip()
    home = Path(config) if config else Path(environ.get('HOME') or Path.home()) / '.claude'
    project = Path(project) if project else Path.cwd()
    paths += [home / 'settings.json',
              project / '.claude' / 'settings.local.json',
              project / '.claude' / 'settings.json']
    return tuple(paths)


def allowlist(environ=None, project=None, files=None):
    """The ``availableModels`` allowlist in force, or ``None`` when there is none.

    Claude Code concatenates this one across the settings files rather than
    letting the highest-precedence file replace the others, so a project file
    naming one model does not hide the two in the user's own settings. Reading
    only the first file would refuse models that are in fact available, which
    is a refusal with nothing wrong behind it.

    Read-only, always: a settings file is the user's, and nothing here writes
    one or suggests editing one.
    """
    allowed = set()
    for path in (files if files is not None else settings_files(environ, project)):
        for name in _settings_models(path) or ():
            allowed |= {name, normalize(name), spawn_name(name)}
    return frozenset(allowed) if allowed else None


def available(model, allowed=None):
    """Whether one model can be asked for: by the allowlist, or by being known."""
    full = normalize(model)
    if allowed is not None:
        return bool({model, full, spawn_name(full)} & set(allowed))
    return full in KNOWN


def problems(assignment, allowed=None):
    """One line per slot whose model cannot be asked for, naming the flag.

    The line is the whole refusal: a reader has the slot, the model it would
    have run and the flag that changes it, and does not have to work out which
    of several settings put that model there.

    There are two refusals and they are not the same thing. A name this version
    of the plugin does not know is not a model anybody's login was asked about
    — only the four in :data:`KNOWN` are named here, by alias or by full id —
    and saying it was unavailable on the login would send a reader to their
    settings over a misspelling. A known model that an ``availableModels``
    allowlist leaves out is the login refusing it.
    """
    lines = []
    for slot, model in assignment.items():
        if available(model, allowed):
            continue
        flag = FLAG[role_of(slot)]
        full = normalize(model)
        if full not in KNOWN:
            lines.append(f'{slot} would run {full}, which is not a model this version knows '
                         f'(known: {", ".join(KNOWN)}, or their aliases '
                         f'{", ".join(sorted(ALIASES))}); change it with {flag} '
                         '(no model was run)')
        else:
            lines.append(f'{slot} would run {full}, which the availableModels allowlist in this '
                         f'login\'s settings does not include; change it with {flag} '
                         '(no model was run)')
    return lines


def refuse(assignment, allowed=None, environ=None, project=None):
    """Raise :class:`ValueError` naming every slot whose model is unavailable.

    Called at startup, before a session is launched and before the manuscript
    is imported, because the alternative is Claude Code substituting a model of
    its own choosing and the run recording an arm it did not run.
    """
    if allowed is None:
        allowed = allowlist(environ=environ, project=project)
    lines = problems(assignment, allowed)
    if lines:
        raise ValueError('\n'.join(lines))
    return assignment


def version_numbers(text):
    """The leading ``x.y.z`` of a version string as a tuple, or ``()``.

    ``claude --version`` prints ``2.1.251 (Claude Code)``, and a build may add
    a suffix after the numbers. Only the three numbers are compared.
    """
    match = re.match(r'\s*v?(\d+)\.(\d+)\.(\d+)', str(text or ''))
    return tuple(int(part) for part in match.groups()) if match else ()


def subagent_override(version, environ=None):
    """The refusal line when ``CLAUDE_CODE_SUBAGENT_MODEL`` would outrank this plan.

    Before Claude Code 2.1.251 that variable outranked both a per-spawn model
    override and an agent file, so with it set every agent in the run would be
    the one model it names, whatever slot asked for what — and the run would
    record the plan rather than that. From 2.1.251 it ranks below both and the
    plan holds, so there is nothing to refuse.

    The variable is the user's and is never unset here: a process that quietly
    removed it would be deciding something the user set deliberately, and a
    session started from a different environment is their call to make. An
    unparseable version is not refused either, because "older than 2.1.251" is
    not something it says.
    """
    environ = os.environ if environ is None else environ
    value = str(environ.get(SUBAGENT_MODEL) or '').strip()
    if not value:
        return None
    numbers = version_numbers(version)
    if not numbers or numbers >= SUBAGENT_PRECEDENCE_FIXED:
        return None
    return (f'{SUBAGENT_MODEL} is set to {value} and this Claude Code is '
            f'{str(version).strip()}, where that variable outranks both the per-spawn model this '
            f'plugin sends and the agent files: every slot would run {normalize(value)} and the '
            f'run would record the models it asked for instead. Claude Code '
            f'{".".join(str(part) for part in SUBAGENT_PRECEDENCE_FIXED)} ranks it below both, so '
            f'upgrade, or start this review from an environment where {SUBAGENT_MODEL} is not set '
            '(no model was run)')


def family_of(name):
    """Which model family one name belongs to: ``sonnet``, ``opus``, and so on.

    A full id, a family alias and a provider-prefixed id all answer the same
    family, which is what :func:`substituted` has to compare: Bedrock and
    Vertex spell the same model differently, and ``ANTHROPIC_DEFAULT_*_MODEL``
    or an alias resolved elsewhere can land a different *version* of the family
    that was asked for. A name with no family in it answers itself, so two
    unknown ids still compare unequal.
    """
    full = normalize(name)
    if full in FAMILY:
        return FAMILY[full]
    text = str(full).strip().casefold()
    if text in ALIASES:
        return text
    for part in re.split(r'[^a-z0-9]+', text):
        if part in ALIASES:
            return part
    return text


def substituted(assigned, observed):
    """Whether ``observed`` is a different *family* from ``assigned``.

    A different family is a substitution: the slot did not run the model the
    run is recorded under, and no amount of reading the files afterwards makes
    it the arm it was supposed to be. A different version of the same family is
    not — it is which version that family resolved to on this login, and the
    observed id is simply what gets reported.
    """
    if not assigned or not observed:
        return False
    return family_of(assigned) != family_of(observed)


def substitution_line(slot, assigned, observed):
    """One sentence naming the slot, what it was assigned and what ran."""
    return (f'{slot} was assigned {normalize(assigned)} but ran {observed}: a different model '
            'family ran in that slot, so this run did not run the models it records')


def substitutions(rows):
    """One line per agent row whose observed model is a different family.

    ``rows`` are :func:`peerreviewagents.engine.usage.agent_rows` rows, where
    ``assigned_model`` is the plan and ``model`` is what the slot reported
    running. A slot with no observation is not a substitution: nothing was seen
    to disagree with.
    """
    lines = []
    for row in rows or ():
        assigned, observed = row.get('assigned_model'), row.get('model')
        if substituted(assigned, observed):
            lines.append(substitution_line(row.get('slot') or 'a slot', assigned, observed))
    return lines


def summary(assignment):
    """The distinct models in one assignment, in the order the slots use them."""
    seen = []
    for model in assignment.values():
        if model not in seen:
            seen.append(model)
    return seen


def describe(assignment):
    """``protocol.json``'s ``model``: a sentence, because consumers read a string."""
    models = summary(assignment)
    if not models:
        return 'per-slot, pinned in the plugin agent files'
    return ', '.join(models) + ' (per slot; see models)'
