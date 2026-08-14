from peerreviewagents.checkpoints import checkpointed


def _state(tmp_path):
    return {
        "manuscript_md": "# Stable manuscript",
        "config": {
            "checkpoint_dir": str(tmp_path),
            "resume": True,
            "reasoning_model": "test/model",
            "max_node_cost_usd": 5.0,
        },
    }


def test_successful_node_is_resumed_without_calling_model(tmp_path):
    calls = []

    def node(_state):
        calls.append(1)
        return {"reports": [{"reviewer": "rigor", "score": 3}]}

    wrapped = checkpointed("reviewer_rigor", node, _state(tmp_path)["config"])
    first = wrapped(_state(tmp_path))
    second = wrapped(_state(tmp_path))

    assert first == second
    assert calls == [1]


def test_failed_node_is_never_checkpointed(tmp_path):
    calls = []

    def node(_state):
        calls.append(1)
        return {"errors": ["provider down"]}

    wrapped = checkpointed("reviewer_rigor", node, _state(tmp_path)["config"])
    wrapped(_state(tmp_path))
    wrapped(_state(tmp_path))
    assert calls == [1, 1]


def test_semantic_config_change_does_not_reuse_checkpoint(tmp_path):
    calls = []

    def node(_state):
        calls.append(1)
        return {"value": len(calls)}

    state1 = _state(tmp_path)
    checkpointed("editor", node, state1["config"])(state1)
    state2 = _state(tmp_path)
    state2["config"]["reasoning_model"] = "other/model"
    checkpointed("editor", node, state2["config"])(state2)
    assert calls == [1, 1]


def test_node_cost_budget_rejects_and_does_not_checkpoint(tmp_path):
    calls = []

    def node(_state):
        calls.append(1)
        return {"total_cost": 6.0, "decision": "major"}

    state = _state(tmp_path)
    wrapped = checkpointed("editor", node, state["config"])
    assert "exceeded" in wrapped(state)["errors"][0]
    assert "exceeded" in wrapped(state)["errors"][0]
    assert calls == [1, 1]
