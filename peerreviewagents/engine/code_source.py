"""The code a paper is reviewed against: snapshotted once, then read only.

A review that is handed the analysis code checks the paper against it — the
threshold the methods section names against the constant the script uses, the
filter the text says was applied against the one the pipeline applies, the
version of a tool against the version pinned. That check is worth nothing
unless the code the reviewer read is the code the run can show afterwards, and
it is worth less than nothing if reviewing a paper means running a stranger's
repository.

So the code is copied in once, at the import, and never executed. What lands
in the run folder is text source and nothing else: a suffix allowlist, a skip
list for the folders that hold dependencies and data, a name rule that keeps
out the files that hold credentials, and three caps — per file, in total, and
in number of files. A notebook is stored as its cells, because the outputs in
one are both most of its bytes and none of its method. Line endings are folded
to LF on the way in and the copy is written and read back with the platform's
own translation turned off, so a CRLF repository is numbered the way its
author's editor numbers it, on whatever machine the review runs on.
:func:`snapshot` writes that copy
under ``<run>/code/files`` and a ``manifest.json`` beside it recording where
the code came from, which commit it was, every file included with its hash and
line count, and every file skipped with the reason. A reader of the run can
therefore say what was reviewed, and can tell "the repository had no such
file" from "the snapshot left it out".

:class:`CodeDesk` is how a model reads it: three tools that list, read and
search the snapshot, in the shape :class:`peerreviewagents.engine.investigation.Investigation`
has, so the server's one dispatch covers them. None of them executes
anything, none of them reaches the network, and none of them can name a path
outside the snapshot. :meth:`CodeDesk.verify` is the other half of the rule
that a code finding carries evidence: the server checks the lines the finding
quotes against the file, and refuses the finding when the text is not there.

**The code is data, not instruction.** A comment in a repository saying to
file a finding, or to ignore one, is a sentence in a file somebody wrote, and
the agent file for the ``code`` track says so. Nothing here interprets the
content it copies.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .storage import digest, read, write

#: The file the snapshot's record is written to, inside the snapshot folder.
MANIFEST_FILE = 'manifest.json'

#: The folder inside the snapshot that holds the copied source, so that a
#: repository with a ``manifest.json`` of its own cannot overwrite the record
#: of the snapshot it is in.
FILES_DIRECTORY = 'files'

#: What a snapshot is, in its ``schema_version``.
SCHEMA_VERSION = 'code-snapshot-v1'

#: The suffixes copied in. Source and configuration a human reads: a reviewer
#: checking the methods section against the code is reading scripts, notebooks
#: stripped to their source, build files and pinned requirements. Anything
#: else — data, archives, images, compiled objects, models — is skipped by not
#: being here, which is why this is an allowlist and not a deny list: a
#: repository holds file types nobody listed, and copying an unknown one into
#: the run folder is how a 400 MB array of measurements gets there.
TEXT_SUFFIXES = (
    '.py', '.pyi', '.R', '.r', '.Rmd', '.jl', '.m', '.do', '.sas',
    '.c', '.h', '.cc', '.cpp', '.hpp', '.cu', '.rs', '.go', '.java', '.kt',
    '.js', '.jsx', '.ts', '.tsx', '.sh', '.bash', '.zsh', '.pl', '.rb', '.lua',
    '.sql', '.f', '.f90', '.for',
    '.toml', '.cfg', '.ini', '.yaml', '.yml', '.json', '.txt', '.md', '.rst',
    '.tex', '.bib', '.nf', '.smk', '.snakefile', '.cmake', '.mk',
    '.ipynb',
)

#: The notebook suffix, handled on its own: a notebook is JSON holding cells,
#: and what this snapshot stores is :func:`notebook_source` of it.
NOTEBOOK_SUFFIX = '.ipynb'

#: Names never copied, whatever their suffix, because of what they hold rather
#: than what kind of file they are. A repository with a ``prod.env`` or a
#: deployment key in it is common, and copying one into the run folder would
#: put a live credential in a file a model is then served. The patterns are
#: matched against the lowercased file name with :mod:`fnmatch`, and a match is
#: skipped with the reason :data:`SECRET_REASON` — said plainly, because a
#: reviewer has to be able to tell "there is no configuration here" from "the
#: configuration was not copied in".
SECRET_PATTERNS = (
    '.env', '*.env', '.env.*', '.netrc', '_netrc', '.npmrc', '.pypirc',
    '.pgpass', '.htpasswd', '.git-credentials', '.dockercfg',
    'credentials*', 'secrets*', 'secret', 'secret.*',
    '*.pem', '*.key', '*.pfx', '*.p12', '*.jks', '*.keystore', '*.ppk',
    '*.crt', '*.cer', '*.der', '*.asc', '*.gpg', '*.kdbx',
    'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*',
    '*_rsa', '*_rsa.pub', '*_dsa', '*_ecdsa', '*_ed25519', '*_ed25519.pub',
)

#: Why a :data:`SECRET_PATTERNS` match is not in the snapshot.
SECRET_REASON = 'may hold secrets'

#: Why a folder, or a symbolic link, is not in the snapshot. Each is recorded
#: once — the folder, not every file under it — so the record says what was
#: never looked at without listing a dependency tree file by file.
DIRECTORY_REASON = 'not looked at: a folder this snapshot does not walk into'
LINK_REASON = 'not looked at: a symbolic link out of the snapshot'

#: Files copied in whatever their suffix, because a build or a pipeline is
#: often in a file that has none.
TEXT_NAMES = (
    'Makefile', 'makefile', 'GNUmakefile', 'Dockerfile', 'Containerfile',
    'CMakeLists.txt', 'Snakefile', 'Rakefile', 'Justfile', 'justfile',
    'LICENSE', 'NOTICE', 'README', 'CITATION', 'nextflow.config',
    'requirements.txt', 'environment.yml', 'renv.lock', 'DESCRIPTION',
    'NAMESPACE',
)

#: Folders never walked into: version control internals, dependency trees,
#: build output, caches, and the places data is kept. A vendored dependency is
#: not the authors' method, and copying one buries the code that is.
SKIPPED_DIRECTORIES = (
    '.git', '.hg', '.svn', '.github', '.idea', '.vscode',
    'node_modules', 'bower_components', 'vendor', 'third_party', 'thirdparty',
    '.venv', 'venv', 'env', '.env', 'virtualenv', 'site-packages',
    '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', '.tox',
    'build', 'dist', 'target', '.next', '.cache', 'renv',
    'data', 'Data', 'datasets', 'raw', 'results', 'output', 'outputs',
    'figures', 'figs', 'logs', '.ipynb_checkpoints',
)

#: The caps. A file over :data:`MAX_FILE_BYTES` is skipped as too large — a
#: generated table with a ``.txt`` suffix is not source — and the walk stops
#: adding files once either the total or the count is reached, recording what
#: it left out. They are what keeps "snapshot the code" from meaning "copy a
#: repository of unknown size into the run folder".
MAX_FILE_BYTES = 200_000
MAX_TOTAL_BYTES = 8_000_000
MAX_FILES = 600

#: The one cap that is read off the file on disk rather than off the text
#: stored. A notebook holding a figure is megabytes of base64 and a few
#: hundred lines of code, so ``max_file_bytes`` is applied to the cells
#: :func:`notebook_source` keeps and not to the file they were in — which
#: would skip the analysis for the size of its own output. This is the ceiling
#: on the file that is parsed to get there.
MAX_NOTEBOOK_BYTES = 20_000_000

#: The line a cell is marked with in the text a notebook is stored as. The
#: ``# %%`` is the cell convention a person reading a notebook as a script
#: already knows, and the number is the cell's own, so code evidence citing a
#: line of the stored text can be read back to a cell of the notebook.
CELL_MARK = '# %% notebook cell {number} ({kind})'

#: How many lines :meth:`CodeDesk.read_code` serves in one part, and how long a
#: line is served before it is clipped. A file is read in parts for the same
#: reason the manuscript is: the whole thing in one answer is most of a
#: context window for a file nobody needed all of.
PART_LINES = 300
MAX_LINE_CHARACTERS = 400

#: How many matches :meth:`CodeDesk.search_code` answers with.
SEARCH_HITS = 40

#: How many lines of code a finding may quote as its evidence. The quote is
#: there to show the reader of the review the thing the finding is about, not
#: to reproduce a module.
MAX_EVIDENCE_LINES = 20

#: How long :func:`clone` waits for git, in seconds.
CLONE_TIMEOUT = 300


class CodeRefused(ValueError):
    """The code a run was given cannot be snapshotted, with the reason why.

    A folder that is not there, a URL git refused, a repository with no text
    source in it at all. The server turns this into the refusal ``ingest``
    answers with, because a run that was asked to review code and silently
    reviewed none would report a code review that never happened.
    """


def is_url(source):
    """Whether *source* names a remote repository rather than a local folder.

    Public, because the launcher asks the same question before it spends a
    probe on a folder that is not there — and the answer has to be the one this
    module will act on, not a second rule written beside it.
    """
    text = str(source)
    if text.startswith(('http://', 'https://', 'git://', 'ssh://')):
        return True
    # `git@host:owner/repo.git`, the scp-like spelling, which has no scheme.
    return bool(re.match(r'^[^/\s]+@[^/\s:]+:[^\s]+$', text))


def clone(source, into):
    """Shallow-clone *source* into *into* and say what was cloned.

    Hooks are off (``core.hooksPath`` pointed at nothing), submodules are not
    recursed into, no tags are fetched, one branch at depth 1 is taken, and
    git is forbidden to ask for credentials. A clone is the one thing here
    that runs another program, and everything a repository could use it to run
    on this machine — a hook, a submodule's own hook, a credential helper
    prompting on a terminal — is turned off rather than trusted.
    """
    into = Path(into)
    into.parent.mkdir(parents=True, exist_ok=True)
    environment = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GIT_ASKPASS': '',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GCM_INTERACTIVE': 'never'}
    command = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'protocol.file.allow=never',
               'clone', '--depth', '1', '--single-branch', '--no-tags',
               '--recurse-submodules=no', '--quiet', str(source), str(into)]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              timeout=CLONE_TIMEOUT, env=environment, check=False)
    except FileNotFoundError as exc:
        raise CodeRefused('git is not installed, so a repository URL cannot be cloned: '
                          + str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeRefused('cloning ' + str(source) + ' took longer than '
                          + str(CLONE_TIMEOUT) + ' seconds') from exc
    if done.returncode != 0:
        said = ' '.join((done.stderr or done.stdout or '').split())[:400]
        raise CodeRefused('git could not clone ' + str(source) + ': ' + (said or 'no reason given'))
    return {'kind': 'git-clone', 'source': str(source), **_git_state(into)}


def _git(folder, *arguments):
    """One read-only git command in *folder*, or ``''`` when it says nothing."""
    try:
        done = subprocess.run(['git', '-C', str(folder), *arguments], capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ''
    return done.stdout.strip() if done.returncode == 0 else ''


def _git_state(folder):
    """The commit, branch and dirtiness of a folder, as far as git will say.

    A folder that is not a checkout answers nothing, which is recorded as
    nothing: a copied directory is a legitimate thing to review, and a
    manifest that invented a commit for one would be worse than a manifest
    that says there is none.
    """
    commit = _git(folder, 'rev-parse', 'HEAD')
    if not commit:
        return {'commit': '', 'branch': '', 'dirty': None, 'remote': ''}
    return {
        'commit': commit,
        'branch': _git(folder, 'rev-parse', '--abbrev-ref', 'HEAD'),
        # A checkout with uncommitted changes is reviewable, and the commit
        # alone would not describe what was read.
        'dirty': bool(_git(folder, 'status', '--porcelain')),
        'remote': _git(folder, 'config', '--get', 'remote.origin.url'),
    }


def _secret(name):
    """Whether a file name is one :data:`SECRET_PATTERNS` keeps out."""
    low = str(name).lower()
    return any(fnmatch.fnmatch(low, pattern) for pattern in SECRET_PATTERNS)


def _wanted(path):
    """Whether one file is text source this snapshot copies, or why it is not.

    The secret-name rule is first, so a ``prod.env`` is left out for what it
    holds rather than kept for what it looks like.
    """
    if _secret(path.name):
        return SECRET_REASON
    if path.name in TEXT_NAMES:
        return None
    if path.suffix in TEXT_SUFFIXES:
        return None
    return 'not text source: ' + (path.suffix or 'no suffix')


def notebook_source(raw):
    """The cells of a notebook as text: code and markdown in order, no outputs.

    A notebook is JSON, and the JSON is mostly output — execution counts,
    stream text, base64 images. Stored raw it is noise a reviewer reads instead
    of the analysis, and usually too large to store at all. So what the
    snapshot keeps is the cells: each one marked with :data:`CELL_MARK` and its
    number, its source as the author wrote it, and nothing the kernel wrote
    back.

    Raises :class:`ValueError` naming the reason when *raw* is not a notebook
    this can read, which is how the walk comes to skip it.
    """
    try:
        held = json.loads(raw)
    except ValueError as exc:
        raise ValueError('not a readable notebook: ' + str(exc)) from exc
    cells = held.get('cells') if isinstance(held, dict) else None
    if not isinstance(cells, list):
        raise ValueError('not a readable notebook: it holds no list of cells')
    pieces = []
    for number, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        kind = str(cell.get('cell_type') or '')
        if kind not in ('code', 'markdown', 'raw'):
            continue
        source = cell.get('source')
        body = (''.join(str(one) for one in source) if isinstance(source, list)
                else str(source or ''))
        pieces.append(CELL_MARK.format(number=number, kind=kind) + '\n' + body.rstrip('\n'))
    if not pieces:
        raise ValueError('not a readable notebook: it holds no code or markdown cells')
    # A cell's own source can hold CRLF even in a notebook file that does not,
    # because the line endings are inside JSON strings.
    return _normalize('\n\n'.join(pieces) + '\n')


def _normalize(text):
    """*text* with every line ending as one ``\\n``.

    Repositories hold CRLF files, and a snapshot of one has to number its
    lines the way the author's editor does: ``\\r\\n`` and a lone ``\\r`` are
    each one line break, not two and not a blank line in between. Every line
    ending is folded here, at the one point text enters the snapshot, so the
    copy under ``files/`` holds LF alone and the line a finding cites is the
    same line on every platform the review runs on.
    """
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _split(text):
    """*text* as its lines, broken on line endings and nothing else.

    ``str.splitlines`` also breaks on a form feed, a vertical tab and a few
    Unicode separators, which an editor shows as a character inside a line. A
    file holding one would be numbered here differently from the editor the
    author quoted it out of, so the count and the numbering both come from
    this.
    """
    lines = _normalize(text).split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def _text(data):
    """*data* decoded as UTF-8 text, or ``None`` when it is not text at all."""
    if b'\0' in data[:4096]:
        return None
    try:
        return _normalize(data.decode('utf-8'))
    except UnicodeDecodeError:
        return None


def _walk(root):
    """The files under *root* to consider, and what was never looked at.

    Returns ``(files, left_out)``. Symbolic links are not followed, for files
    or directories: a link is a path into somewhere this snapshot was not
    given. A skipped folder and a link are each recorded once, by their own
    path rather than by the files inside them, so the record can say what was
    not looked at without a vendored dependency filling it.
    """
    found, left_out = [], []
    for folder, directories, files in os.walk(root, followlinks=False):
        kept = []
        for name in sorted(directories):
            path = Path(folder, name)
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                left_out.append({'path': relative, 'reason': LINK_REASON})
            elif name in SKIPPED_DIRECTORIES:
                left_out.append({'path': relative, 'reason': DIRECTORY_REASON})
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            path = Path(folder, name)
            if path.is_symlink():
                left_out.append({'path': path.relative_to(root).as_posix(),
                                 'reason': LINK_REASON})
            else:
                found.append(path)
    return found, left_out


def snapshot(source, folder, *, limits=None):
    """Copy the text source of *source* into *folder* and write its manifest.

    *source* is a local folder or a repository URL; a URL is cloned first, into
    a temporary checkout beside the snapshot that is removed once the text is
    copied, so what the run folder keeps is the text and the record of it and
    not a working tree. Returns the manifest, which is also written to
    ``<folder>/manifest.json``:

    ``origin``
        ``kind`` (``folder`` or ``git-clone``), the ``source`` as given, and
        the ``commit``, ``branch``, ``dirty`` flag and ``remote`` where git
        would say — empty strings and ``None`` for a plain folder.
    ``files``
        Every file copied, in path order: its ``path`` relative to the
        snapshot, ``bytes``, ``lines`` and ``sha256``, plus ``extracted`` on a
        file whose stored text was pulled out of something else — a notebook,
        stored as its cells.
    ``skipped``
        Everything left out, in path order, with ``path`` and ``reason``: not
        text source, may hold secrets, too large, not decodable as text, past
        one of the caps, or a folder or symbolic link never looked at — those
        two recorded once each, by the folder or the link and not by the files
        under it.
    ``counts``, ``limits``, ``truncated``
        How many files and bytes were copied, the caps in force, and whether a
        cap stopped the walk — a reviewer reading a snapshot that was cut short
        has to be told so.

    Raises :class:`CodeRefused` when there is nothing to review: a folder that
    is not there, a URL git refuses, or a repository holding no text source.
    """
    folder = Path(folder)
    caps = {'max_file_bytes': MAX_FILE_BYTES, 'max_total_bytes': MAX_TOTAL_BYTES,
            'max_files': MAX_FILES, **(limits or {})}
    folder.mkdir(parents=True, exist_ok=True)
    files_root = folder / FILES_DIRECTORY
    if is_url(source):
        checkout = folder / '.checkout'
        if checkout.exists():
            shutil.rmtree(checkout)
        origin = clone(source, checkout)
        root, temporary = checkout, True
    else:
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise CodeRefused('No code folder at ' + str(root)
                              + '. Give a folder that exists or a repository URL.')
        origin = {'kind': 'folder', 'source': str(root), **_git_state(root)}
        temporary = False
    try:
        record = _copy(root, files_root, caps)
    finally:
        if temporary:
            shutil.rmtree(root, ignore_errors=True)
    if not record['files']:
        raise CodeRefused('No text source in ' + str(origin['source'])
                          + ': every file in it was skipped, so there is no code to review. '
                          + 'Left out ' + str(len(record['skipped'])) + ' path(s).')
    manifest = {'schema_version': SCHEMA_VERSION, 'origin': origin,
                'limits': caps, **record}
    write(folder / MANIFEST_FILE, manifest)
    return manifest


def _contents(path, caps):
    """What this snapshot would store for one file, or why it stores nothing.

    Either ``{'reason': …}`` or the fields the manifest row is built from:
    ``text`` as it is written into the snapshot, its ``bytes`` and ``sha256``,
    and ``extracted`` saying what the text was pulled out of when it is not
    the file itself.
    """
    if path.suffix == NOTEBOOK_SUFFIX:
        return _notebook_contents(path, caps)
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {'reason': 'could not be read: ' + str(exc)}
    if size > caps['max_file_bytes']:
        return {'reason': 'larger than ' + str(caps['max_file_bytes']) + ' bytes'}
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {'reason': 'could not be read: ' + str(exc)}
    text = _text(data)
    if text is None:
        return {'reason': 'not decodable as UTF-8 text'}
    # The digest and the byte count are of the text as the snapshot stores it,
    # which is the file with its line endings folded to LF: a reader can
    # recompute them from the copy under ``files/``.
    stored = text.encode('utf-8')
    return {'text': text, 'bytes': len(stored), 'sha256': digest(stored), 'extracted': ''}


def _notebook_contents(path, caps):
    """A notebook as its cells, with the file cap applied to the cells kept."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        return {'reason': 'could not be read: ' + str(exc)}
    if size > MAX_NOTEBOOK_BYTES:
        return {'reason': 'larger than ' + str(MAX_NOTEBOOK_BYTES)
                + ' bytes, which is too large a notebook to read the cells out of'}
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {'reason': 'could not be read: ' + str(exc)}
    raw = _text(data)
    if raw is None:
        return {'reason': 'not decodable as UTF-8 text'}
    try:
        text = notebook_source(raw)
    except ValueError as exc:
        return {'reason': str(exc)}
    kept = len(text.encode('utf-8'))
    if kept > caps['max_file_bytes']:
        return {'reason': 'larger than ' + str(caps['max_file_bytes'])
                + ' bytes even with its outputs dropped'}
    return {'text': text, 'bytes': kept, 'sha256': digest(text.encode('utf-8')),
            'extracted': 'notebook cells'}


def _copy(root, files_root, caps):
    """The walk itself: what was copied, what was skipped, and why."""
    found, left_out = _walk(root)
    files, skipped = [], list(left_out)
    total = 0
    truncated = ''
    for path in found:
        relative = path.relative_to(root).as_posix()
        reason = _wanted(path)
        if reason:
            skipped.append({'path': relative, 'reason': reason})
            continue
        held = _contents(path, caps)
        if 'reason' in held:
            skipped.append({'path': relative, 'reason': held['reason']})
            continue
        if len(files) >= caps['max_files']:
            truncated = truncated or ('more than ' + str(caps['max_files'])
                                      + ' files of text source')
            skipped.append({'path': relative, 'reason': 'past the file count cap'})
            continue
        if total + held['bytes'] > caps['max_total_bytes']:
            truncated = truncated or ('more than ' + str(caps['max_total_bytes'])
                                      + ' bytes of text source')
            skipped.append({'path': relative, 'reason': 'past the total size cap'})
            continue
        destination = files_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        # ``newline`` pinned to LF: the default translates every ``\n`` to the
        # platform's ending, which on Windows turned a CRLF file into one
        # blank line between every line of code and doubled the numbers a
        # finding has to cite.
        destination.write_text(held['text'], encoding='utf-8', newline='\n')
        total += held['bytes']
        row = {'path': relative, 'bytes': held['bytes'],
               'lines': len(_split(held['text'])), 'sha256': held['sha256']}
        if held['extracted']:
            # A reader of the manifest has to know the lines a finding cites
            # are lines of the cells and not of the file on disk.
            row['extracted'] = held['extracted']
        files.append(row)
    skipped.sort(key=lambda one: one['path'])
    return {'files': files, 'skipped': skipped, 'truncated': truncated,
            'counts': {'files': len(files), 'bytes': total, 'skipped': len(skipped)}}


def _clip(line):
    """One line as it is served: long ones cut, with what was cut said."""
    if len(line) <= MAX_LINE_CHARACTERS:
        return line
    return line[:MAX_LINE_CHARACTERS] + ' … [' + str(len(line)) + ' characters]'


class CodeDesk:
    """The snapshot, read only, in the shape the server's dispatch expects.

    Three tools and nothing else: :meth:`list_code`, :meth:`read_code` and
    :meth:`search_code`. There is no tool here that writes, that runs
    anything, that reaches the network, or that can be pointed outside the
    snapshot — a path is resolved against the manifest's own file list, so a
    ``../`` is not a path this desk has.

    :attr:`opened` is the set of files a holder has read, which is what the run
    reports as "files opened out of files in the snapshot": a code review that
    opened two files out of four hundred is a different thing from one that
    read the pipeline, and a reader of the review can see which it was.
    """

    def __init__(self, folder):
        self.folder = Path(folder)
        self.manifest = read(self.folder / MANIFEST_FILE)
        self.files_root = self.folder / FILES_DIRECTORY
        self.rows = {row['path']: row for row in self.manifest.get('files') or []}
        self.opened = set()
        self.searches = []

    # --- what the run records ---------------------------------------------

    @property
    def origin(self):
        return dict(self.manifest.get('origin') or {})

    def record(self):
        """What the run folder and the report say about this code review."""
        origin = self.origin
        return {
            'reviewed': True,
            'kind': str(origin.get('kind') or ''),
            'source': str(origin.get('source') or ''),
            'commit': str(origin.get('commit') or ''),
            'branch': str(origin.get('branch') or ''),
            'dirty': origin.get('dirty'),
            'snapshot': str(self.folder),
            'files_in_snapshot': len(self.rows),
            'files_skipped': len(self.manifest.get('skipped') or []),
            'truncated': str(self.manifest.get('truncated') or ''),
            'files_opened': sorted(self.opened),
            'opened': len(self.opened),
            'searches': len(self.searches),
        }

    # --- the tools ---------------------------------------------------------

    def call(self, name, args):
        """One code tool by name, with the server's argument dict."""
        tool = {'list_code': self.list_code, 'read_code': self.read_code,
                'search_code': self.search_code}.get(name)
        if tool is None:
            return {'request_rejected': str(name) + ' is not one of the code tools: '
                                        + 'list_code, read_code, search_code.'}
        try:
            return tool(**dict(args))
        except (ValueError, KeyError, TypeError, OSError, re.error) as exc:
            return {'request_rejected': str(exc)}

    def _path(self, path):
        """The manifest row for *path*, or a refusal naming what is there.

        The snapshot's own list is the only way in, which is what makes a path
        outside it unreachable rather than guarded against.
        """
        wanted = str(path or '').strip().lstrip('./')
        row = self.rows.get(wanted)
        if row is None:
            near = [name for name in self.rows if wanted and wanted in name][:8]
            raise ValueError(
                (str(path) or 'an empty path') + ' is not a file in the code snapshot. '
                + ('Did you mean: ' + ', '.join(near) + '? ' if near else '')
                + 'Call list_code for what is there.')
        return row

    def _lines(self, row):
        # Read with the newline translation off and split here, so one rule
        # numbers the lines however the file reached the snapshot.
        held = (self.files_root / row['path']).read_text(encoding='utf-8', newline='')
        return _split(held)

    def list_code(self, path='', pattern=''):
        """The files in the snapshot, with their sizes, and what was left out."""
        prefix = str(path or '').strip().lstrip('./')
        glob = str(pattern or '').strip()
        rows = [row for row in self.manifest.get('files') or []
                if (not prefix or row['path'] == prefix or row['path'].startswith(prefix + '/'))
                and (not glob or Path(row['path']).match(glob))]
        return {
            'origin': self.origin,
            'files': [{'path': row['path'], 'lines': row['lines'], 'bytes': row['bytes'],
                       'extracted': str(row.get('extracted') or '')}
                      for row in rows],
            'count': len(rows),
            'files_in_snapshot': len(self.rows),
            # What is not here, so that "the code does not do that" is never
            # read off a snapshot that simply left the file out.
            'skipped': [dict(one) for one in (self.manifest.get('skipped') or [])[:40]],
            'skipped_count': len(self.manifest.get('skipped') or []),
            'truncated': str(self.manifest.get('truncated') or ''),
        }

    def read_code(self, path, part=1):
        """One file of the snapshot, as numbered lines, in parts.

        The line numbers are the file's own, and they are what a finding's code
        evidence cites. Reading a file is recorded, because a review says how
        much of the code it opened.

        ``extracted`` says when the lines are not the lines of the file on
        disk: a notebook is served as its cells, each marked with its cell
        number, with every output dropped.
        """
        row = self._path(path)
        lines = self._lines(row)
        parts = max(1, -(-len(lines) // PART_LINES))
        if type(part) is not int or isinstance(part, bool) or not 1 <= part <= parts:
            raise ValueError(row['path'] + ' is served in ' + str(parts)
                             + ' part(s), so part must be 1 to ' + str(parts) + '.')
        first = (part - 1) * PART_LINES + 1
        served = lines[first - 1:first - 1 + PART_LINES]
        self.opened.add(row['path'])
        return {
            'path': row['path'],
            'part': part,
            'of': parts,
            'first_line': first,
            'last_line': first + len(served) - 1,
            'lines': row['lines'],
            'sha256': row['sha256'],
            'extracted': str(row.get('extracted') or ''),
            'text': '\n'.join(str(first + index) + '\t' + _clip(line)
                              for index, line in enumerate(served)),
        }

    def search_code(self, query, regex=False, path=''):
        """Find a phrase, or a regular expression, in the snapshot by line."""
        wanted = str(query or '')
        if len(wanted.strip()) < 2:
            raise ValueError('search_code takes a phrase of at least two characters.')
        prefix = str(path or '').strip().lstrip('./')
        pattern = re.compile(wanted if regex else re.escape(wanted))
        hits, searched = [], 0
        for row in self.manifest.get('files') or []:
            if prefix and not (row['path'] == prefix or row['path'].startswith(prefix + '/')):
                continue
            searched += 1
            for number, line in enumerate(self._lines(row), start=1):
                if pattern.search(line):
                    hits.append({'path': row['path'], 'line': number, 'text': _clip(line.strip())})
                    if len(hits) >= SEARCH_HITS:
                        break
            if len(hits) >= SEARCH_HITS:
                break
        self.searches.append(wanted)
        return {'query': wanted, 'regex': bool(regex), 'files_searched': searched,
                'matches': hits, 'count': len(hits),
                'complete': len(hits) < SEARCH_HITS,
                'note': ('The first ' + str(SEARCH_HITS) + ' matches. Narrow the search or '
                         'name a path.') if len(hits) >= SEARCH_HITS else ''}

    # --- what makes a code finding evidence --------------------------------

    def verify(self, path, start_line, end_line, text):
        """Check that *text* is exactly what the snapshot holds at those lines.

        This is the server's check, not a tool: a finding against the code
        carries the file, the line range and the text, and it is stored only if
        the text is the text at those lines. Whitespace at the ends of lines
        and the kind of line ending are not part of the comparison; everything
        else is. Returns the stored evidence, or raises :class:`ValueError`
        with what the lines actually hold.
        """
        row = self._path(path)
        lines = self._lines(row)
        for name, value in (('start_line', start_line), ('end_line', end_line)):
            if type(value) is not int or isinstance(value, bool) or not 1 <= value <= len(lines):
                raise ValueError(name + ' must be a line of ' + row['path'] + ', 1 to '
                                 + str(len(lines)) + '.')
        if end_line < start_line:
            raise ValueError('end_line is before start_line.')
        if end_line - start_line + 1 > MAX_EVIDENCE_LINES:
            raise ValueError('A code finding quotes at most ' + str(MAX_EVIDENCE_LINES)
                             + ' lines. Quote the lines the finding is about.')
        held = lines[start_line - 1:end_line]
        given = _split(str(text or ''))
        if [one.rstrip() for one in given] != [one.rstrip() for one in held]:
            raise ValueError(
                'The code you quoted is not what ' + row['path'] + ' holds at lines '
                + str(start_line) + '-' + str(end_line) + '. It holds:\n'
                + '\n'.join(str(start_line + index) + '\t' + _clip(line)
                            for index, line in enumerate(held))
                + '\nQuote those lines exactly, or cite the lines your quote is from.')
        self.opened.add(row['path'])
        return {'path': row['path'], 'start_line': start_line, 'end_line': end_line,
                'text': '\n'.join(held), 'sha256': row['sha256']}


def origin_name(source):
    """What a snapshot records as the code it was taken from.

    A URL is kept as it was given, because that is the name of the remote. A
    local path is resolved, so the same folder named two ways — a relative path,
    a symlink, a ``~`` — is one source and not two.
    """
    text = str(source)
    return text if is_url(text) else str(Path(text).expanduser().resolve())


def attach(source, folder):
    """The desk on *source* snapshotted in *folder*, taken once and reused after.

    A run folder is re-opened: ``ingest`` is safe to call twice, and a killed
    run is recovered by opening the same folder again. The snapshot is taken on
    the first call only. Re-cloning on the second would spend the network
    again and, worse, could copy a different commit under a review that may
    already hold findings citing the lines of the first one — so a folder that
    already holds a snapshot of the same code keeps it, and a folder holding a
    snapshot of *different* code is refused rather than overwritten.
    """
    folder = Path(folder)
    asked = origin_name(source)
    kept = folder / MANIFEST_FILE
    if kept.is_file():
        manifest = read(kept)
        held = str((manifest.get('origin') or {}).get('source') or '')
        if held != asked:
            raise CodeRefused(
                str(folder) + ' already holds a snapshot of ' + (held or 'other code')
                + ', and this run was given ' + asked + '. One folder holds one code '
                'snapshot: review the other code in a fresh out folder.')
        if not (manifest.get('files') or []):
            raise CodeRefused('The code snapshot in ' + str(folder) + ' holds no files.')
        return CodeDesk(folder)
    snapshot(source, folder)
    return CodeDesk(folder)
