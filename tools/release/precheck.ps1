$ErrorActionPreference = 'Stop'
& node "$PSScriptRoot/precheck.mjs" @args
exit $LASTEXITCODE
