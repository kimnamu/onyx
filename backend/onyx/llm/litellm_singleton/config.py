import litellm

from onyx.utils.logger import remove_litellm_native_log_handlers, setup_logger

logger = setup_logger()


def configure_litellm_settings() -> None:
    # If a user configures a different model and it doesn't support all the same
    # parameters like frequency and presence, just ignore them
    litellm.drop_params = True
    litellm.telemetry = False  # ty: ignore[invalid-assignment]
    litellm.modify_params = True
    litellm.add_function_to_prompt = False
    litellm.suppress_debug_info = True
    # LiteLLM records must flow only through the app logging pipeline, not also
    # through the stream handler LiteLLM attaches at import.
    remove_litellm_native_log_handlers()


def initialize_litellm() -> None:
    configure_litellm_settings()
