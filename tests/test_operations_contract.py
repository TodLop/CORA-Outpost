import pytest

from app.services.operations import OperationNotFound, get_operation_spec


def test_unknown_operation_rejected():
    with pytest.raises(OperationNotFound):
        get_operation_spec("nope:does-not-exist")


def test_server_start_operation_has_permission_metadata():
    spec = get_operation_spec("server:start")
    assert spec.required_permission == "server:start"
