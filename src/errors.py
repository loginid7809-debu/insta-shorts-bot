"""Error types shared by the pipeline.

The distinction that matters operationally:

  * **Transient**  - Instagram rate-limited us, a network timeout, YouTube quota
    exhausted, a 5xx from Google. Nothing is wrong with the URL. The row must stay
    in the queue and keep retrying, and it must **not** consume an attempt - with
    hourly runs a transient error would otherwise send a good video to Failed
    within three hours.
  * **Permanent**  - private/deleted post, unsupported URL, video longer than the
    Shorts limit, a rejected upload request. Retrying will never help, so attempts
    are counted and the row is moved to the Failed tab after `max_attempts`.
"""


class ShortsBotError(RuntimeError):
    """Base class. `transient = False` means 'do not retry forever'."""

    transient = False


class TransientError(ShortsBotError):
    """Worth retrying on the next hourly run; never counts as a failed attempt."""

    transient = True
