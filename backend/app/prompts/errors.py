"""Errors raised by the prompt store."""


class PromptError(Exception):
    """Base class for every prompt-store error."""


class PromptKeyError(PromptError):
    """The prompt key does not match the required format."""


class PromptTemplateError(PromptError):
    """A template body has invalid syntax or mismatched variables."""


class PromptNotFoundError(PromptError):
    """No prompt version or active pointer matches the request."""
