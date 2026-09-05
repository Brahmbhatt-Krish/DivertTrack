# Locates a real bash.exe from a Git for Windows install, checking the
# well-known install locations directly (a user profile path containing a
# space, e.g. "Krish Brahmbhatt", breaks Make's own $(wildcard)/$(firstword)
# argument splitting, and inline cmd/PowerShell one-liners embedded in a
# Makefile are too fragile to quote correctly across shells - a plain script
# file sidesteps both problems). Falls back to PATH if none of them exist.
# Prints the resolved path, or nothing if bash cannot be found at all.
$candidates = @(
    (Join-Path $env:ProgramFiles 'Git\bin\bash.exe'),
    (Join-Path $env:ProgramFiles 'Git\usr\bin\bash.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Git\usr\bin\bash.exe')
)
$found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($found) {
    Write-Output $found
} else {
    $onPath = Get-Command bash -ErrorAction SilentlyContinue
    if ($onPath) { Write-Output $onPath.Source }
}
