from __future__ import annotations


class CustomAdapterNotImplementedError(NotImplementedError):
    pass


def execute_custom_adapter(*, adapter_name: str, action_name: str) -> None:
    raise CustomAdapterNotImplementedError(
        f"custom adapters are a future escape hatch only; no adapter is implemented for "
        f"{adapter_name}.{action_name}"
    )
