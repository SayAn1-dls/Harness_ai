from lcc.github_pr import DEFAULT_PERMISSIONS, GitHubPermission, assert_permitted


def test_merge_not_in_default():
    assert GitHubPermission.MERGE_PR not in DEFAULT_PERMISSIONS
    try:
        assert_permitted(GitHubPermission.MERGE_PR, DEFAULT_PERMISSIONS | {GitHubPermission.MERGE_PR})
        raise AssertionError("merge should still be denied")
    except PermissionError:
        pass
