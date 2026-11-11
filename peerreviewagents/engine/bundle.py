"""The one-command review, shared by every surface that offers it.

``peerreview review manuscript.pdf run-output`` imports the PDF into
``run-output/manuscript``, reviews it in ``run-output/review`` and writes the
two reports into ``run-output``. The command line, the MCP server and the local
web UI all offer that job, so the layout, the refusals and the report-on-failure
behaviour live here once rather than three times.

Nothing here prints or exits. A caller-visible mistake raises ``BundleError``
with the sentence a person should read; the command line turns that into
``parser.error`` and the server returns it to its client.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .storage import digest, read, write

# The one-command form's layout inside the run folder a person names, and the
# statuses that mean a run spent its calls and produced a review.
MANUSCRIPT_DIRECTORY='manuscript'
RUN_DIRECTORY='review'
MAX_RUNS=99
FINISHED_STATUSES=('complete','complete-without-findings')

# Every accepted review mode, and the three worth choosing between on a surface
# a person drives. plain, workspace and self-revision are study instruments:
# they still run, and docs/study-arms.md names the command for each arm.
REVIEW_MODES=('ensemble','independent','plain','workspace','panel','self-revision')
PUBLIC_MODES=('ensemble','independent','panel')
MAX_REVIEWERS=16


class BundleError(ValueError):
    """A manuscript, run folder or report path a person has to fix."""


@dataclass(frozen=True)
class ReviewPaths:
    """Where one review reads, runs and reports.

    ``bundle`` is the folder that holds ``manuscript/``, ``review/`` and the
    reports. It is ``None`` when an already imported workspace was named, which
    is the original two-step behaviour: that run writes no reports of its own.
    ``imported`` is the paper record when this call did the import, and ``None``
    when the workspace already held it.
    """
    workspace:Path
    run:Path
    bundle:Optional[Path]=None
    imported:Optional[dict]=None

    def record(self):
        return {'workspace':str(self.workspace),'run':str(self.run),
                'bundle':None if self.bundle is None else str(self.bundle)}


def looks_like_pdf(path):
    """A manuscript PDF, by its suffix or by the header a PDF actually has."""
    if path.suffix.lower()=='.pdf':
        return True
    try:
        with path.open('rb') as handle:
            return handle.read(5)==b'%PDF-'
    except OSError:
        return False


def run_progress(run):
    """Whatever a run directory last recorded about itself."""
    path=Path(run)/'progress.json'
    return read(path) if path.is_file() else {}


def import_manuscript(source,workspace):
    """Import the PDF once, and refuse a different PDF in an existing bundle.

    Returns the imported paper record, or ``None`` when the workspace already
    holds this manuscript and the import is reused. The imported manuscript is
    immutable and the review is anchored to it, so a second PDF in the same run
    folder is a mistake rather than a new run. The comparison is against the
    exact bytes the first import kept.
    """
    source,workspace=Path(source),Path(workspace)
    supplied=digest(source.read_bytes())
    original=workspace/'original.pdf'
    if (workspace/'paper.json').is_file():
        if original.is_file() and digest(original.read_bytes())!=supplied:
            raise BundleError(f'{workspace} already holds a different manuscript. Name a new run '
                              f'folder for {source}')
        return None
    from .ingest import import_paper
    existed=workspace.exists()
    try:
        return import_paper(source,workspace)
    except Exception as error:
        # A half-imported workspace reads as a manuscript to the next command,
        # and the original.pdf in it would make the refusal above meaningless.
        if not existed:
            from shutil import rmtree
            rmtree(workspace,ignore_errors=True)
        raise BundleError(f'{source} could not be imported: {type(error).__name__}: {error}') from error


def run_directory(bundle):
    """The folder this review writes, leaving every earlier run in place.

    A review directory is never reused: the engine refuses it, and a second run
    over a finished review would spend the subscription again for a report the
    folder already has. A finished run therefore stops the command and names
    its report; a failed or interrupted one is kept and the new attempt runs in
    ``review-2``, ``review-3`` and so on.
    """
    bundle=Path(bundle)
    for index in range(1,MAX_RUNS+1):
        run=bundle/(RUN_DIRECTORY if index==1 else f'{RUN_DIRECTORY}-{index}')
        if not run.exists():
            return run
        if run_progress(run).get('status') in FINISHED_STATUSES:
            raise BundleError(f'{run} already holds a finished review, reported in '
                              f'{bundle/"REVIEW.md"}. Name a different run folder to review again')
    raise BundleError(f'{bundle} already holds {MAX_RUNS} review runs. Name a different run folder')


def default_output(paper):
    """The run folder a review writes when the command names none.

    ``peerreview manuscript.pdf`` is the shortest path to a review, so the
    folder is predictable from the manuscript's own name and sits beside it:
    ``manuscript-review``, holding the import, the run and the two reports. A
    second run of the same command reuses the import and writes ``review-2``,
    exactly as naming the folder again would.
    """
    source=Path(paper)
    return source.parent/f'{source.name if source.is_dir() else source.stem}-review'


def plan_review(paper,output=None):
    """Resolve a manuscript PDF or an imported workspace into review paths.

    A PDF is imported into ``<output>/manuscript`` and the run is placed beside
    it, which is the one-command review. An existing workspace directory keeps
    exactly the previous behaviour: it is reviewed in place, into ``output``
    itself, and no reports are written by the run.

    Re-running the same one-command review reuses the import and never reviews
    the same manuscript twice by accident: see ``run_directory``.

    Without an ``output``, the folder is ``default_output(paper)``, which is
    what the bare ``peerreview manuscript.pdf`` review writes.
    """
    source=Path(paper)
    if output is None:
        output=default_output(source)
    if source.is_dir():
        if not (source/'paper.json').is_file():
            raise BundleError(f'{paper} is a directory with no paper.json, so it is not an imported '
                              'manuscript workspace. Name the manuscript PDF instead')
        return ReviewPaths(source,Path(output))
    if not source.exists():
        raise BundleError(f'{paper} does not exist. Name a manuscript PDF, or a workspace directory '
                          'already imported with peerreview import')
    if not source.is_file() or not looks_like_pdf(source):
        raise BundleError(f'{paper} is not a PDF. Name a manuscript PDF, or a workspace directory '
                          'already imported with peerreview import')
    bundle=Path(output)
    imported=import_manuscript(source,bundle/MANUSCRIPT_DIRECTORY)
    return ReviewPaths(bundle/MANUSCRIPT_DIRECTORY,run_directory(bundle),bundle,imported)


def review_options(mode='ensemble',reviewers=2,provider=None,model=None,policy=None,
                   token_budget=None,literature_cutoff=None,source_images=()):
    """Validate one review request from a surface that has no argument parser.

    The command line does this through ``argparse``, which reports its own
    errors. A server or a web form has to be told the same refusals in the same
    words, so they are here once: the accepted modes, the reviewer bound, the
    single-session policy that cannot run an ensemble, a model that belongs to
    another transport and a malformed cutoff. Returns the keyword arguments
    ``run_review_options`` runs.

    Presets are deliberately not accepted. They are the study arms, which
    docs/study-arms.md gives as command lines, and expanding them here would be
    a second place for what a preset pins to drift.
    """
    from .config import default_literature_cutoff
    from .models import DEFAULT_MODELS, DEFAULT_PROVIDER, provider_for
    from .review_policy import DEFAULT_POLICY_NAME, get_review_policy, review_policies, single_session_refusal
    if mode not in REVIEW_MODES:
        raise BundleError(f'unknown review mode {mode!r}. Available: {list(PUBLIC_MODES)}, and the '
                          'study modes in docs/study-arms.md')
    if mode!='ensemble' and (reviewers!=2 or token_budget is not None):
        raise BundleError('reviewers and token_budget apply only to ensemble mode')
    if type(reviewers) is not int or not 1<=reviewers<=MAX_REVIEWERS:
        raise BundleError(f'reviewers must be a whole number from 1 to {MAX_REVIEWERS}')
    if token_budget is not None and (type(token_budget) is not int or token_budget<1):
        raise BundleError('token_budget must be a positive whole number of observed tokens')
    if provider is not None and provider not in DEFAULT_MODELS:
        raise BundleError(f'unknown provider {provider!r}. Available: {sorted(DEFAULT_MODELS)}')
    if model is None:
        model=DEFAULT_MODELS[provider or DEFAULT_PROVIDER]
    else:
        try:
            serves=provider_for(model)
        except ValueError as error:
            raise BundleError(f'{model} is not a supported subscription model') from error
        if provider is not None and serves!=provider:
            raise BundleError(f'{model} is not a {provider} model')
    policy=policy or DEFAULT_POLICY_NAME
    if policy not in review_policies():
        raise BundleError(f'unknown review policy {policy!r}. Available: {list(review_policies())}')
    if get_review_policy(policy).single_session and mode=='ensemble':
        # Spelled the way a caller with no command line writes it, which is
        # the one thing that differs from the command line's own refusal.
        raise BundleError(single_session_refusal(policy,option='policy',preset='preset',
                                                 mode="mode='independent'"))
    if literature_cutoff is None:
        literature_cutoff=default_literature_cutoff()
    else:
        import datetime
        try:
            datetime.date.fromisoformat(literature_cutoff)
        except (TypeError,ValueError) as error:
            raise BundleError('literature_cutoff must be a date, YYYY-MM-DD') from error
    return {'literature_cutoff':literature_cutoff,'mode':mode,'model':model,'reviewers':reviewers,
            'token_budget':token_budget,'shared_images':list(source_images),
            'synthesis_policy':'debate','policy':policy}


def run_review_options(paths,options):
    """Run one review from the keyword arguments ``review_options`` returned."""
    options=dict(options)
    return run_bundle_review(paths,options.pop('literature_cutoff'),options.pop('mode'),**options)


def recorded_cutoff(run):
    """The cutoff a run directory already recorded, if it recorded one.

    A run that is being continued keeps its own cutoff, so continuing it on a
    later day is not refused as changed inputs.
    """
    protocol=Path(run)/'protocol.json'
    return read(protocol).get('literature_cutoff') if protocol.is_file() else None


def run_bundle_review(paths,literature_cutoff,mode='ensemble',**options):
    """Run one review and, for a bundle, report it whatever happened.

    The one-command form owns the whole job, so a run that stops still leaves
    the two reports and a cause a person can read. Returns the progress record,
    the report paths and ``failure``: the exception sentence, or ``None`` when
    the run finished. A named workspace keeps raising instead, so no existing
    script loses its traceback.
    """
    from .reviewer import review
    failure=None
    try:
        progress=review(str(paths.workspace),str(paths.run),literature_cutoff,mode,**options)
    except Exception as error:
        if paths.bundle is None:raise
        failure=f'{type(error).__name__}: {error}'
        progress=run_progress(paths.run)
        if not progress.get('status'):
            # A run blocked before its first call — no subscription login, a
            # missing CLI — recorded nothing, and the report written from that
            # said 'Run status: unknown' instead of the cause. Record the cause
            # as the run's own state, which is what every reader reads.
            progress={'status':'blocked','error':failure}
            if Path(paths.run).is_dir():
                write(Path(paths.run)/'progress.json',progress)
    reports={}
    if paths.bundle is not None and (Path(paths.run)/'protocol.json').is_file():
        # The reports are written from the saved run, with no further model
        # calls. A blocked run is reported as blocked rather than left with no
        # report at all.
        from .run_report import write_reports
        reports={kind:str(path)
                 for kind,path in write_reports(str(paths.run),str(paths.workspace),
                                                str(paths.bundle)).items()}
    return {'progress':progress,'reports':reports,'failure':failure,**paths.record()}


def review_outcome(progress):
    """What a finished run means: its status, and whether it is reportable.

    A run can finish every call and still endorse nothing, either because the
    reviewers found nothing or because every finding was quarantined by the
    source guards. That is a real outcome with a report, not a failure, and
    every surface has to say so rather than claim a review was delivered.
    """
    from .reviewer import COMPLETE_WITHOUT_FINDINGS,GUARD_STATUS_POLICY
    if not isinstance(progress,dict):
        return {'status':None,'without_findings':False,'note':None}
    without=(progress.get('status')==COMPLETE_WITHOUT_FINDINGS
             or progress.get('review_guard_status')=='all-findings-quarantined')
    return {'status':progress.get('status'),'without_findings':without,
            'note':GUARD_STATUS_POLICY if without else None}
