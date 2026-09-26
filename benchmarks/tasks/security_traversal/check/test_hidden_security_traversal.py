import pytest
from files import UPLOAD_DIR, read_upload

def test_normal_and_nested():
    assert read_upload("hello.txt") == "hello\n"
    assert read_upload("2024/report.txt") == "q1 numbers\n"

@pytest.mark.parametrize("name", ["../secrets.txt", "2024/../../secrets.txt", "/etc/hosts"])
def test_traversal_rejected(name):
    with pytest.raises(ValueError):
        read_upload(name)

def test_absolute_inside_is_not_escape_vector():
    with pytest.raises(ValueError):
        read_upload(str(UPLOAD_DIR.parent / "secrets.txt"))
