from app.handlers.handlers_state_start import _session_for_new_generation


def test_new_generation_does_not_inherit_run_character_override():
    original={'selected_project_key':'discipline','generation_run_id':'old',
              'story_character_references':{'hero':{'scope_run_id':'old','selected_path':'first.png'}},
              'story_character_bible':[{'id':'hero','asset_uri':'first.png'}]}
    new=_session_for_new_generation(original)
    assert new['selected_project_key']=='discipline'
    assert 'story_character_references' not in new and 'story_character_bible' not in new
    assert original['story_character_references']['hero']['scope_run_id']=='old'
