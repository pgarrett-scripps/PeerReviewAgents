"""Which model runs in which slot: the assignment, the refusal, the record.

Stage A ran three core reviewers on one model and they caught no more than one
of them did, so the reviewers are deliberately different models. That is a
claim about the arm a run was, which means the assignment has to be decided in
one place, refused when a model is not available rather than substituted, and
written into the run folder so a published review can say which model wrote
which report.

Every test here runs offline. Nothing starts Claude Code and no model is run:
the server makes no model calls at all, and the assignment is settled before
the manuscript is imported.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from peerreviewagents.plugin import models as model_plan
from peerreviewagents.plugin import server as srv
from peerreviewagents.runs import read_run

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'single-column.pdf'
SONNET, OPUS, HAIKU, FABLE = (
    'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-haiku-5-5', 'claude-fable-5-2')


@pytest.fixture(autouse=True)
def no_allowlist(monkeypatch):
    """No ``availableModels`` anywhere, which is the ordinary login.

    A machine that does restrict model choice would otherwise refuse the
    defaults and fail every test here for a reason that is not the test's.
    """
    monkeypatch.setattr(model_plan, 'allowlist', lambda **kwargs: None)
    srv.STATE['run'] = None
    srv.STATE['plugin_root'] = None
    yield
    srv.STATE['run'] = None


def opened(tmp_path, folder='run', **kwargs):
    answer = srv.ingest(str(FIXTURE), str(tmp_path / folder), **kwargs)
    srv.STATE['run'] = None  # each ingest here is a fresh server process
    return answer


def models_of(answer):
    return {row['slot']: row['model'] for row in answer.get('models', [])}


# -- the ordering -----------------------------------------------------------


@pytest.mark.parametrize('reviewers, wanted', [
    (1, [SONNET]),
    (2, [SONNET, OPUS]),
    (3, [SONNET, OPUS, HAIKU]),
    (5, [SONNET, OPUS, HAIKU, SONNET, OPUS]),
])
def test_one_reviewer_is_sonnet_and_three_are_three_different_models(reviewers, wanted):
    """The documented default: 1 is Sonnet, 2 adds Opus, 3 adds Haiku, 4 starts again."""
    slots = tuple(f'core-{index}' for index in range(1, reviewers + 1))
    assignment = model_plan.assign(slots)
    assert [assignment[slot] for slot in slots] == wanted
    if reviewers >= 3:
        assert len(set(assignment.values())) == 3, 'three reviewers are three readers'


def test_the_specialists_the_editor_and_the_recorder_have_their_own_defaults():
    assignment = model_plan.assign(('spec-stats', 'spec-citations', 'editor', 'recorder'))
    assert assignment == {'spec-stats': SONNET, 'spec-citations': SONNET,
                          'editor': FABLE, 'recorder': HAIKU}


def test_a_revision_runs_the_model_it_is_revising():
    """``revision-1`` is ``core-1`` reading its own findings again, not a new reader."""
    assignment = model_plan.assign(('core-1', 'core-2', 'revision-1'))
    assert assignment['revision-1'] == assignment['core-1'] == SONNET
    assert assignment['core-2'] == OPUS


def test_the_list_is_taken_in_order_and_aliases_are_resolved():
    assignment = model_plan.assign(('core-1', 'core-2', 'core-3'),
                                   reviewers='opus, haiku,sonnet')
    assert [assignment[f'core-{i}'] for i in (1, 2, 3)] == [OPUS, HAIKU, SONNET]
    assert model_plan.assign(('core-1', 'core-2'), reviewers=['haiku'])['core-2'] == HAIKU


def test_an_empty_choice_is_not_a_choice():
    """A blank flag or variable means "I did not choose", never "no models"."""
    assert model_plan.parse_list('') == ()
    assert model_plan.parse_list(None) == ()
    assert model_plan.assign(('core-1',), reviewers='')['core-1'] == SONNET
    assert model_plan.assign(('editor',), editor='  ')['editor'] == FABLE


def test_an_empty_entry_inside_a_list_is_refused_rather_than_dropped():
    """``sonnet,,haiku`` silently moved reviewer 2 onto Haiku. It stops the run.

    Dropping the empty item answers a typo by running a different arm from the
    one that was typed, and nothing in the run would say so.
    """
    for typed in ('sonnet,,haiku', ',sonnet', 'sonnet, ,haiku', 'sonnet,'):
        with pytest.raises(ValueError) as refused:
            model_plan.parse_list(typed)
        assert 'empty entry' in str(refused.value)
        assert 'no model was run' in str(refused.value)
    assert model_plan.parse_list('sonnet, opus') == (SONNET, OPUS), 'a space after a comma'
    assert model_plan.parse_list('sonnet opus') == (SONNET, OPUS), 'spaces, with no comma'


def test_the_arguments_and_the_environment_are_merged_in_one_place():
    """:func:`settle` is the only merge, so two readers cannot disagree.

    The starter planned from its flags while handing the environment to the
    session, so a session launched with ``PEERREVIEW_REVIEWER_MODELS`` set and
    no flag given wrote the defaults into the folder and then refused that same
    folder for holding a run under other models.
    """
    environ = {'PEERREVIEW_REVIEWER_MODELS': 'haiku', 'PEERREVIEW_EDITOR_MODEL': 'opus'}
    assert model_plan.settle(environ=environ) == {'reviewers': [HAIKU], 'editor': OPUS}
    # A flag is this run's own choice; a variable is what the environment was
    # left in. Only the slots somebody chose are in the answer at all.
    chosen = model_plan.settle(reviewers='sonnet,opus', recorder='fable', environ=environ)
    assert chosen == {'reviewers': [SONNET, OPUS], 'editor': OPUS, 'recorder': FABLE}
    assert model_plan.settle(environ={}) == {}, 'nobody chose anything'


def test_the_override_is_the_family_and_the_record_is_the_version():
    """A per-spawn override takes an alias; the run records the version."""
    assert model_plan.spawn_name(OPUS) == 'opus'
    assert model_plan.spawn_name('opus') == 'opus'
    assert model_plan.normalize('fable') == FABLE


# -- the refusal ------------------------------------------------------------


def test_a_model_this_login_cannot_run_is_refused_by_slot_flag_and_name():
    with pytest.raises(ValueError) as refused:
        model_plan.refuse(model_plan.assign(('core-1', 'core-2'), reviewers='sonnet,gpt-9'))
    line = str(refused.value)
    assert 'core-2' in line and 'gpt-9' in line
    assert '--reviewer-models' in line
    assert 'no model was run' in line


def test_each_slot_names_the_flag_that_changes_it():
    assignment = model_plan.assign(('core-1', 'spec-stats', 'editor', 'recorder'),
                                   reviewers='nope-1', specialist='nope-2',
                                   editor='nope-3', recorder='nope-4')
    lines = model_plan.problems(assignment, allowed=None)
    assert len(lines) == 4
    joined = '\n'.join(lines)
    for flag in ('--reviewer-models', '--specialist-model', '--editor-model',
                 '--recorder-model'):
        assert flag in joined


def test_an_allowlist_in_the_settings_decides_what_is_available(tmp_path, monkeypatch):
    """Availability is Claude Code's own ``availableModels``, concatenated.

    Claude Code adds up the ``availableModels`` of every settings file it reads
    rather than letting the first one win, so a model named in any of them is
    one this login may run. Reading it the other way would refuse a run the
    login could have made — a refusal the user cannot act on, because the model
    really is available.
    """
    monkeypatch.undo()  # this test is about the allowlist, so it reads real files
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / 'settings.json').write_text(
        json.dumps({'availableModels': ['sonnet', 'haiku']}), encoding='utf-8')
    project = tmp_path / 'project'
    (project / '.claude').mkdir(parents=True)
    (project / '.claude' / 'settings.json').write_text(
        json.dumps({'availableModels': ['opus']}), encoding='utf-8')

    allowed = model_plan.allowlist(environ={'HOME': str(home)}, project=project)
    assert model_plan.available(SONNET, allowed) and model_plan.available('haiku', allowed)
    assert model_plan.available(OPUS, allowed), 'every file that names a model counts'
    assert not model_plan.available(FABLE, allowed), 'a model no file names is not available'
    lines = model_plan.problems(model_plan.assign(('editor',)), allowed)
    assert len(lines) == 1 and FABLE in lines[0] and '--editor-model' in lines[0]

    nothing = model_plan.allowlist(environ={'HOME': str(tmp_path / 'empty')},
                                   project=tmp_path / 'empty')
    assert nothing is None, 'no allowlist is no restriction, not an empty one'


def test_an_old_claude_code_with_the_subagent_variable_set_is_refused():
    """Before 2.1.251 that variable outranked the per-spawn model and the files.

    Every slot would have run the one model it names while the run recorded the
    plan. The variable is the user's, so it is never unset here: the refusal
    says to upgrade or to start the review from an environment without it.
    """
    set_to = {'CLAUDE_CODE_SUBAGENT_MODEL': 'haiku'}
    line = model_plan.subagent_override('2.1.250', environ=set_to)
    assert 'CLAUDE_CODE_SUBAGENT_MODEL' in line and 'haiku' in line
    assert HAIKU in line and '2.1.250' in line and '2.1.251' in line
    assert 'no model was run' in line

    assert model_plan.subagent_override('2.1.251', environ=set_to) is None
    assert model_plan.subagent_override('2.1.322', environ=set_to) is None
    assert model_plan.subagent_override('2.2.0', environ=set_to) is None
    assert model_plan.subagent_override('2.1.250', environ={}) is None
    assert model_plan.subagent_override('2.1.250', environ={'CLAUDE_CODE_SUBAGENT_MODEL': ' '}) \
        is None
    # A version nobody could read cannot answer the only question the check
    # asks, which is whether this run's per-slot plan will hold. Reading
    # silence as "new enough" would run the whole review on one model and
    # record the plan, which is the outcome being prevented.
    for unreadable in ('unknown', '', None, 'Claude Code'):
        refused = model_plan.subagent_override(unreadable, environ=set_to)
        assert refused and 'could not be read' in refused
        assert 'CLAUDE_CODE_SUBAGENT_MODEL' in refused and HAIKU in refused
        assert 'no model was run' in refused
    assert model_plan.subagent_override('unknown', environ={}) is None, \
        'an unreadable version with the variable unset is nothing to refuse'


def test_the_agent_files_pin_the_same_models_this_module_defaults_to():
    """The frontmatter is the fallback, so it must not disagree with the plan.

    A per-spawn override is what normally decides a slot's model; the agent
    file's ``model:`` is what runs when none arrives — a dropped relay, a
    version of Claude Code that ignores the override. Two answers in two places
    is how a run comes to record a model it did not run, so the nine files and
    the defaults here are checked against each other rather than by hand.
    """
    agents = Path(model_plan.__file__).resolve().parent / 'pra' / 'agents'
    pinned = {}
    for path in sorted(agents.glob('*.md')):
        found = re.search(r'^model:\s*(\S+)\s*$', path.read_text(encoding='utf-8'), re.M)
        assert found, f'{path.name} pins no model, so its slot would inherit one'
        pinned[path.stem] = found.group(1)

    assert len(pinned) == 9, pinned
    assert pinned['core-reviewer'] == model_plan.DEFAULT_REVIEWER_MODELS[0]
    assert pinned['editor'] == model_plan.DEFAULT_EDITOR_MODEL
    assert pinned['recorder'] == model_plan.DEFAULT_RECORDER_MODEL
    specialists = {name: model for name, model in pinned.items()
                   if name not in ('core-reviewer', 'editor', 'recorder')}
    assert set(specialists) == set(srv.SPECIALIST_TRACKS), 'one file per specialist track'
    assert set(specialists.values()) == {model_plan.DEFAULT_SPECIALIST_MODEL}
    assert set(pinned.values()) <= set(model_plan.KNOWN)


def test_nothing_in_the_module_gates_on_billing_or_usage():
    source = Path(model_plan.__file__).read_text(encoding='utf-8').lower()
    for word in ('budget', 'cost', 'price', 'quota', 'spend'):
        assert word not in source.replace('this module', ''), word


# -- through the server -----------------------------------------------------


def test_ingest_settles_the_models_and_hands_back_the_spawn_override(tmp_path):
    answer = opened(tmp_path, reviewers=3)
    assert models_of(answer) == {
        'core-1': SONNET, 'core-2': OPUS, 'core-3': HAIKU,
        **{f'spec-{track}': SONNET for track in srv.SPECIALIST_TRACKS},
        'editor': FABLE, 'recorder': HAIKU}
    spawns = {row['slot']: row['spawn'] for row in answer['models']}
    assert spawns['core-2'] == 'opus' and spawns['editor'] == 'fable'


def test_the_flags_reach_every_slot_through_ingest(tmp_path):
    answer = opened(tmp_path, reviewers=2, reviewer_models=['haiku', 'opus'],
                    specialist_model='opus', editor_model='sonnet')
    assignment = models_of(answer)
    assert [assignment['core-1'], assignment['core-2']] == [HAIKU, OPUS]
    assert assignment['spec-stats'] == OPUS
    assert assignment['editor'] == SONNET
    assert assignment['recorder'] == HAIKU, 'the recorder was not asked about'


def test_the_launcher_passes_the_choice_in_the_environment(tmp_path, monkeypatch):
    """A workflow that passes no model arguments still runs what the user chose."""
    monkeypatch.setenv('PEERREVIEW_REVIEWER_MODELS', 'haiku')
    monkeypatch.setenv('PEERREVIEW_EDITOR_MODEL', 'opus')
    assignment = models_of(opened(tmp_path, reviewers=2))
    assert assignment['core-1'] == assignment['core-2'] == HAIKU
    assert assignment['editor'] == OPUS

    # An argument the workflow does pass wins over the environment.
    again = models_of(opened(tmp_path, folder='two', reviewers=1, reviewer_models=['sonnet']))
    assert again['core-1'] == SONNET


def test_a_blank_variable_leaves_the_defaults_alone(tmp_path, monkeypatch):
    monkeypatch.setenv('PEERREVIEW_REVIEWER_MODELS', '')
    monkeypatch.setenv('PEERREVIEW_EDITOR_MODEL', '   ')
    assignment = models_of(opened(tmp_path, reviewers=2))
    assert [assignment['core-1'], assignment['core-2']] == [SONNET, OPUS]
    assert assignment['editor'] == FABLE


def test_ingest_refuses_an_unavailable_model_before_importing_anything(tmp_path):
    refused = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=2,
                         reviewer_models=['sonnet', 'claude-nonesuch-9'])
    assert 'claude-nonesuch-9' in refused['request_rejected']
    assert '--reviewer-models' in refused['request_rejected']
    assert srv.STATE['run'] is None
    assert not (tmp_path / 'run').exists(), 'a refused model writes no folder'


def test_a_folder_run_under_other_models_is_not_pooled_with_this_one(tmp_path):
    opened(tmp_path, reviewers=1)
    refused = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                         reviewer_models=['opus'])
    assert 'different models' in refused['request_rejected']
    assert 'core-1' in refused['request_rejected']
    srv.STATE['run'] = None
    resumed = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=1)
    assert 'request_rejected' not in resumed, 'the same models attach to the same folder'


def test_a_folder_from_before_models_were_recorded_is_left_alone(tmp_path):
    opened(tmp_path, reviewers=1)
    protocol = tmp_path / 'run' / 'review' / 'protocol.json'
    kept = json.loads(protocol.read_text(encoding='utf-8'))
    kept.pop('models')
    protocol.write_text(json.dumps(kept), encoding='utf-8')
    again = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=1,
                       reviewer_models=['opus'])
    assert 'request_rejected' not in again, 'a pre-0.9.0 folder says nothing about models'


def test_a_second_caller_asking_for_other_models_is_not_attached_to_this_run(tmp_path):
    """The models are as much the plan as the arm is.

    Answering ``attached: true`` would hand that caller a live run whose reports
    are recorded under models it did not ask for. A caller that names no models,
    or the same ones, is attaching to this run and is told so.
    """
    srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=2)
    refused = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=2,
                         reviewer_models=['opus'])
    assert 'already runs' in refused['request_rejected']
    assert 'core reviewers' in refused['request_rejected']
    assert SONNET in refused['request_rejected'] and OPUS in refused['request_rejected']
    assert '--reviewer-models' in refused['request_rejected']
    assert 'attached' not in refused

    recorder = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=2,
                          recorder_model='opus')
    assert 'recorder' in recorder['request_rejected'], 'every slot, not only the reviewers'

    same = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=2,
                      reviewer_models=[SONNET, OPUS, HAIKU], editor_model='fable')
    assert same['attached'] is True, 'the same models are the same run'


def test_an_attach_is_not_told_the_slots_or_their_models(tmp_path):
    srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=1)
    again = srv.ingest(str(FIXTURE), str(tmp_path / 'run'), reviewers=1)
    assert again['attached'] is True
    assert 'models' not in again and 'slots' not in again


# -- the record -------------------------------------------------------------


def test_the_run_folder_says_which_model_each_slot_was_asked_to_run(tmp_path):
    opened(tmp_path, reviewers=3)
    review = tmp_path / 'run' / 'review'
    protocol = json.loads((review / 'protocol.json').read_text(encoding='utf-8'))
    assert protocol['models']['core-2'] == OPUS
    assert protocol['reviewer_models'] == [SONNET, OPUS, HAIKU]
    assert protocol['plan']['models'] == protocol['models']
    assert OPUS in protocol['model'], 'the string every older consumer reads still lists them'
    assert protocol['effort'] == 'per-agent, pinned in the plugin agent files'
    manifest = json.loads((review / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['plan']['models'] == protocol['models']

    reading = read_run(review)
    assert reading['identity']['models']['editor'] == FABLE
    assert reading['identity']['reviewer_models'] == [SONNET, OPUS, HAIKU]


def test_read_run_says_nothing_about_the_models_of_a_run_from_before_0_9_0(tmp_path):
    opened(tmp_path, reviewers=1)
    review = tmp_path / 'run' / 'review'
    protocol = json.loads((review / 'protocol.json').read_text(encoding='utf-8'))
    for key in ('models', 'reviewer_models'):
        protocol.pop(key)
    for key in ('models', 'reviewer_models'):
        protocol['plan'].pop(key)
    (review / 'protocol.json').write_text(json.dumps(protocol), encoding='utf-8')
    manifest = json.loads((review / 'manifest.json').read_text(encoding='utf-8'))
    for key in ('models', 'reviewer_models'):
        manifest['plan'].pop(key)
    (review / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    identity = read_run(review)['identity']
    assert identity['models'] == {} and identity['reviewer_models'] == []
