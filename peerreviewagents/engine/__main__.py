"""Command line for the evidence-linked review engine.

``python -m peerreviewagents.engine <command>`` and the engine commands of the
``peerreview`` entry point share this parser.
"""
import argparse
from pathlib import Path

from .config import (default_literature_cutoff, get_review_preset, preset_synthesis_policy,
                     review_presets)
from .models import DEFAULT_MODEL, DEFAULT_MODELS, DEFAULT_PROVIDER, SUBSCRIPTION_MODELS, provider_for
from .review_policy import (COMMAND_LINE_POLICY_NAME, DEFAULT_POLICY_NAME, get_review_policy,
                            review_policies, single_session_refusal)
from .storage import read

MODEL_HELP=('Exact model ID. Without it, the default for --provider is used: '+
            ', '.join(f'{model} for {provider}' for provider,model in DEFAULT_MODELS.items())+
            f'. Without --provider either, that is {DEFAULT_MODEL} on {DEFAULT_PROVIDER}.')

COMMANDS = ('import', 'verify', 'add-review', 'result-map', 'review', 'investigate',
            'report', 'checks')

CUTOFF_HELP = 'Latest eligible publication date, YYYY-MM-DD. Defaults to today'

# Every accepted review mode, and the three worth choosing between on the
# command line. plain, workspace and self-revision are study instruments: they
# still run, and docs/study-arms.md names the command for each arm.
REVIEW_MODES = ('ensemble', 'independent', 'plain', 'workspace', 'panel', 'self-revision')
PUBLIC_MODES = ('ensemble', 'independent', 'panel')
PUBLIC_MODES_METAVAR = '{' + ','.join(PUBLIC_MODES) + '}'


def add_model_arguments(parser):
    parser.add_argument('--provider',choices=tuple(DEFAULT_MODELS),
                        help=f'Subscription transport (default {DEFAULT_PROVIDER})')
    parser.add_argument('--model',choices=SUBSCRIPTION_MODELS,help=MODEL_HELP)


def add_policy_arguments(parser):
    parser.add_argument('--policy',choices=tuple(review_policies()),
        help=f'Named review policy (default {COMMAND_LINE_POLICY_NAME}: high effort, a thirty call tool '
             'allowance, no finding cap and one bounded retry in every stage). '
             f'{DEFAULT_POLICY_NAME} is the bounded study budget of at most five findings in about 900 '
             'words at medium effort. workspace-agentic-v1 is the same agentic allowance for one single '
             'long session, so it needs --mode independent')
    parser.add_argument('--preset',choices=tuple(review_presets()),
        help='Named review preset. It pins --mode, --reviewers, --token-budget and --policy, so pass '
             'it alone. The model comes from --provider, which defaults to '+DEFAULT_PROVIDER)


def apply_preset(parser,args):
    """Resolve --preset into the review arguments it pins, rejecting conflicts."""
    if args.preset is None:
        return
    preset=get_review_preset(args.preset)
    if preset.workflow=='manuscript-only':
        parser.error(f'--preset {args.preset} is a manuscript-only workflow. Run it through '
                     'peerreviewagents.engine.api.review_request_from_preset')
    if (args.mode,args.reviewers,args.token_budget)!=('ensemble',2,750000):
        parser.error('--preset sets --mode, --reviewers and --token-budget. Pass the preset on its own')
    if args.policy is not None and args.policy!=preset.policy:
        parser.error(f'--policy {args.policy} conflicts with the {preset.policy} policy its preset pins')
    if preset.model is not None and args.model is not None and args.model!=preset.model:
        parser.error(f'--model {args.model} conflicts with the {preset.model} model its preset pins')
    args.mode,args.reviewers=preset.workflow,preset.reviewers
    args.token_budget,args.policy=preset.token_budget,preset.policy
    # The preset's synthesis policy is part of what it pins. Without this the
    # ordinary-synthesis arm could be named but never actually ran.
    args.synthesis_policy=preset_synthesis_policy(preset)
    # A preset that pins no model leaves --model and --provider to decide, so the
    # preset alone runs on the default transport instead of a hard-coded one.
    if preset.model is not None:
        args.model=preset.model


def apply_default_policy(args):
    """Name the command line default policy once a preset has had its say.

    A review asked for from the command line runs the agentic allowance: high
    effort, a thirty call tool budget, no finding cap and one bounded retry in
    every stage. ``--policy standard-v1`` asks for the bounded study budget
    instead, and a preset pins its own policy, which is why this runs after
    ``apply_preset``.
    """
    if args.policy is None:
        args.policy = COMMAND_LINE_POLICY_NAME


def apply_policy(parser,args):
    """Refuse a single-session policy left on the default multi-call ensemble.

    ``--policy workspace-agentic-v1`` used to run a two reviewer ensemble plus a
    debate stage, which is three long high-effort calls rather than the one long
    investigative session the policy names. Ask for the mode explicitly.

    The request object and ``ensemble_review`` refuse the same combination, so
    no entry point can spend three long calls on it.
    """
    if args.policy is None:
        return
    if get_review_policy(args.policy).single_session and args.mode=='ensemble':
        parser.error(single_session_refusal(args.policy))


def resolve_cutoff(parser,args,resume=None):
    """Fill the cutoff when none was named, and reject a malformed one.

    A run directory that already recorded a protocol keeps that run's cutoff, so
    continuing an interrupted run on a later day is not refused as changed
    inputs. Today's date is used only for a directory with no protocol.
    """
    if args.literature_cutoff is None:
        stored=None
        if resume is not None:
            protocol=Path(resume)/'protocol.json'
            if protocol.is_file():
                stored=read(protocol).get('literature_cutoff')
        args.literature_cutoff=stored or default_literature_cutoff()
        return
    import datetime
    try:
        datetime.date.fromisoformat(args.literature_cutoff)
    except ValueError:
        parser.error('--literature-cutoff must be a date, YYYY-MM-DD')


def resolve_model(parser,args):
    """Fill the provider default and reject a model that belongs to another provider."""
    if args.model is None:
        args.model=DEFAULT_MODELS[args.provider or DEFAULT_PROVIDER]
    elif args.provider and provider_for(args.model)!=args.provider:
        parser.error(f'{args.model} is not a {args.provider} model')


def prepare_manuscript(parser,args):
    """Import a PDF named in place of a workspace and return the bundle folder.

    ``peerreview review manuscript.pdf run-output`` is the whole review: the PDF
    is imported into ``run-output/manuscript``, the review runs in
    ``run-output/review``, and the two reports are written into ``run-output``.
    Naming an existing workspace directory instead keeps exactly the previous
    behaviour and returns ``None``, so no existing run or script changes.

    An already imported bundle is left alone, so re-running the same command
    resumes the review against the same immutable manuscript.
    """
    source=Path(args.workspace)
    if source.is_dir() or source.suffix.lower()!='.pdf':
        return None
    if not source.is_file():
        parser.error(f'{args.workspace} is neither a manuscript workspace nor a PDF file')
    bundle=Path(args.output)
    workspace=bundle/'manuscript'
    if not (workspace/'paper.json').is_file():
        from .ingest import import_paper
        paper=import_paper(source,workspace)
        print(paper['title'])
        print(paper['statistics'])
    args.workspace,args.output=str(workspace),str(bundle/'review')
    return bundle


def add_subcommands(sub):
    """Attach every engine command to an existing argparse subparser group."""
    p=sub.add_parser('import',help='Import a PDF into a manuscript workspace of source blocks')
    p.add_argument('pdf')
    p.add_argument('output')
    p.add_argument('--xml')
    p.add_argument('--title')
    p.add_argument('--source-url',default='')
    p.add_argument('--supplements')
    p=sub.add_parser('verify',help='Verify a manuscript workspace against its manifest')
    p.add_argument('workspace')
    p=sub.add_parser('add-review',help='Normalize and save an external review response')
    p.add_argument('workspace')
    p.add_argument('response')
    p.add_argument('--id',required=True)
    p=sub.add_parser('result-map',help='Map reported results to their source blocks')
    p.add_argument('workspace')
    p.add_argument('--output',required=True)
    p=sub.add_parser('review',help='Review a manuscript PDF, or an imported workspace, end to end')
    p.add_argument('workspace',metavar='paper',
                   help='Manuscript PDF to import and review, or an already imported workspace')
    p.add_argument('output',help='Run folder. For a PDF it holds manuscript/, review/ and the reports')
    p.add_argument('--literature-cutoff',default=None,
                   help=CUTOFF_HELP+', or the cutoff an existing run directory already recorded')
    # Every mode the engine has is still accepted. Only the three a person
    # should choose between are advertised; the study arms that use the others
    # are written out command by command in docs/study-arms.md.
    p.add_argument('--mode',choices=REVIEW_MODES,default='ensemble',metavar=PUBLIC_MODES_METAVAR,
                   help='Review workflow (default ensemble). The remaining study modes are '
                        'documented in docs/study-arms.md')
    p.set_defaults(synthesis_policy='debate')
    p.add_argument('--reviewers',type=int,default=2,help='Independent discovery reviewers for ensemble mode, from 1 to 16 (default 2)')
    p.add_argument('--token-budget',type=int,default=750000,help='Ensemble observed-token launch ceiling, not a hard cap on a running call')
    add_model_arguments(p)
    add_policy_arguments(p)
    p.add_argument('--source-image',action='append',default=[],help='Original manuscript figure page to attach, at most ten')
    p=sub.add_parser('investigate',help='Run one focused evidence investigation with at most two tools')
    p.add_argument('workspace')
    p.add_argument('output')
    p.add_argument('--question',required=True)
    p.add_argument('--block',action='append',required=True,help='Whole source block to include, repeat up to sixteen times')
    p.add_argument('--literature-cutoff',default=None,help=CUTOFF_HELP)
    add_model_arguments(p)
    p=sub.add_parser('report',help='Write REVIEW.md and REVIEW.txt from a review run, without model calls')
    p.add_argument('run',help='Output folder of a review run')
    p.add_argument('--workspace',help='Reviewed workspace. Found from the run when omitted')
    p.add_argument('--output-dir',help='Folder for the two reports. Defaults to the run folder')
    p=sub.add_parser('checks',help='Record offline prose and readability checks for a workspace')
    p.add_argument('workspace')
    p.add_argument('--scaffold-root',required=True)
    p.add_argument('--output',required=True)
    return sub


def build_parser(prog='peerreviewagents.engine'):
    parser=argparse.ArgumentParser(prog=prog,
        description='Build and inspect a local annotated manuscript workspace')
    add_subcommands(parser.add_subparsers(dest='command',required=True))
    return parser


def dispatch(parser,args):
    """Run one parsed engine command. The parser is used only for argument errors."""
    if args.command=='investigate':
        resolve_cutoff(parser,args)
        resolve_model(parser,args)
        from .focused_investigation import investigate
        memo=investigate(args.workspace,args.output,args.literature_cutoff,
                         args.question,args.block,model=args.model)
        print({'ready_for_adjudication':memo['ready_for_adjudication'],'output':str(Path(args.output)/'memo.json')})
    elif args.command=='import':
        from .ingest import import_paper
        paper=import_paper(args.pdf,args.output,args.xml,args.title,args.source_url,args.supplements)
        print(paper['title'])
        print(paper['statistics'])
    elif args.command=='add-review':
        from .annotations import save_review
        print(save_review(args.workspace,read(args.response),args.id)['annotation_counts'])
    elif args.command=='review':
        bundle=prepare_manuscript(parser,args)
        resolve_cutoff(parser,args,args.output)
        from .reviewer import review
        if args.mode!='ensemble' and (args.reviewers!=2 or args.token_budget!=750000):
            parser.error('--reviewers and --token-budget apply only to ensemble mode')
        apply_preset(parser,args)
        apply_default_policy(args)
        apply_policy(parser,args)
        resolve_model(parser,args)
        progress=review(args.workspace,args.output,args.literature_cutoff,args.mode,
                        model=args.model,reviewers=args.reviewers,
                        token_budget=args.token_budget,shared_images=args.source_image,
                        synthesis_policy=args.synthesis_policy,policy=args.policy)
        print(progress)
        if bundle is not None:
            # The one-command form finishes the job: the reports are written
            # from the saved run, with no further model calls.
            from .run_report import write_reports
            paths=write_reports(args.output,args.workspace,bundle)
            print({kind:str(path) for kind,path in paths.items()})
        from .reviewer import COMPLETE_WITHOUT_FINDINGS,GUARD_STATUS_POLICY
        if isinstance(progress,dict) and (progress.get('status')==COMPLETE_WITHOUT_FINDINGS
                or progress.get('review_guard_status')=='all-findings-quarantined'):
            print(GUARD_STATUS_POLICY)
            raise SystemExit(2)
    elif args.command=='report':
        from .run_report import write_reports
        paths=write_reports(args.run,args.workspace,args.output_dir)
        print({kind:str(path) for kind,path in paths.items()})
    elif args.command=='checks':
        from .scaffold_checks import build_report,compact_summary
        from .storage import write
        report=build_report(args.workspace,args.scaffold_root)
        write(args.output,report)
        print(compact_summary(report))
    elif args.command=='result-map':
        from .annotations import verify_workspace
        from .result_map import build_result_map
        from .storage import write
        result = build_result_map(verify_workspace(args.workspace))
        write(args.output,result)
        print(result['coverage'])
    else:
        from .annotations import verify_workspace
        print('Verified manuscript '+verify_workspace(args.workspace)['id'])


def main(argv=None):
    parser=build_parser()
    dispatch(parser,parser.parse_args(argv))


if __name__=='__main__':
    main()
