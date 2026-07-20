# API reference

The public API is small: activate the import hook once, then read what Lucen
decided through the fallback report and the errors mode. Everything below is
importable from the top-level `lucen` package.

## Activation

::: lucen.activate

::: lucen.deactivate

For a script you launch directly, skip `activate()` and run the file with the
`lucen run` command, which rewrites and executes it in one step.

## Reading what happened

::: lucen.get_fallback_report

::: lucen.clear_fallback_report

::: lucen.get_collected_errors

## Error mode

::: lucen.set_errors_mode

::: lucen.get_errors_mode

::: lucen.ErrorsMode

## Exceptions

::: lucen.LucenError

::: lucen.ClauseValueError
