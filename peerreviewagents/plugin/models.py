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
files Claude Code reads, which is where an organisation restricts model choice.
Where no allowlist is set, the models this plugin knows (:data:`KNOWN`) are
available and any other name is refused: a name nobody here recognises is
exactly the name Claude Code would quietly replace.
"""

from __future__ import annotations

import json
import os
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

    Blank entries are dropped rather than becoming a slot with no model, and an
    empty string means "unset", not "an empty list": the caller then keeps the
    default.
    """
    if text is None:
        return ()
    if isinstance(text, (list, tuple)):
        parts = [str(part) for part in text]
    else:
        parts = str(text).replace(' ', ',').split(',')
    return tuple(normalize(part) for part in parts if str(part).strip())


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

    The first file that names one wins, which is Claude Code's own order:
    managed settings cannot be loosened by a user or project file.
    """
    for path in (files if files is not None else settings_files(environ, project)):
        names = _settings_models(path)
        if names:
            allowed = set()
            for name in names:
                allowed |= {name, normalize(name), spawn_name(name)}
            return frozenset(allowed)
    return None


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
    """
    lines = []
    for slot, model in assignment.items():
        if available(model, allowed):
            continue
        flag = FLAG[role_of(slot)]
        lines.append(f'{slot} would run {normalize(model)}, which is not available on this '
                     f'Claude Code login; change it with {flag} (no model was run)')
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
