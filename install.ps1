# Installs Daimon on Windows:
#   irm https://github.com/LegoManACM/daimon/releases/latest/download/install.ps1 | iex
# $env:DAIMON_VERSION = 'X.Y.Z' picks a version (default: the latest release), $env:DAIMON_NO_SETUP = '1' skips
# `daimon setup`, $env:DAIMON_DRY_RUN = '1' prints what it would run instead of running it, and
# $env:DAIMON_RELEASE_DIR = '<folder>' installs from release files on disk (to test a build before publishing).
# Works in Windows PowerShell 5.1 and PowerShell 7. It never calls `exit`, so piping it into iex can't close your shell.

& {
    $ErrorActionPreference = 'Stop'
    $Repo = 'LegoManACM/daimon'
    $DryRun = [bool]$env:DAIMON_DRY_RUN
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    function Invoke-Native([string]$Exe, [string[]]$Arguments) {
        if ($DryRun) {
            Write-Host "+ $Exe $($Arguments -join ' ')" -ForegroundColor DarkGray
            return
        }
        & $Exe @Arguments
        if ($LASTEXITCODE -ne 0) { throw "``$Exe $($Arguments -join ' ')`` failed (exit code $LASTEXITCODE)." }
    }

    function Find-Uv {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
        foreach ($dir in @($env:XDG_BIN_HOME, "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            if ($dir -and (Test-Path (Join-Path $dir 'uv.exe'))) { return (Join-Path $dir 'uv.exe') }
        }
        return $null
    }

    try {
        Write-Host ''
        Write-Host '  Daimon, a personal agent that works in the background' -ForegroundColor Cyan
        Write-Host '  ---------------------------------------------------' -ForegroundColor Cyan
        Write-Host ''
        if ($DryRun) { Write-Host 'Dry run: printing the commands instead of running them.' -ForegroundColor DarkGray }

        $uv = Find-Uv
        if (-not $uv) {
            Write-Host 'Installing uv (it installs Python and Daimon for you)...'
            if ($DryRun) {
                Write-Host '+ irm https://astral.sh/uv/install.ps1 | iex' -ForegroundColor DarkGray
                $uv = 'uv'
            } else {
                powershell -NoProfile -ExecutionPolicy ByPass -Command 'irm https://astral.sh/uv/install.ps1 | iex'
                if ($LASTEXITCODE -ne 0) { throw "Installing uv failed (exit code $LASTEXITCODE)." }
                $uv = Find-Uv
                if (-not $uv) { throw "uv was installed but can't be found. Open a new terminal and run this again." }
                $env:Path = "$(Split-Path $uv);$env:Path"
            }
        }

        $local = "$env:DAIMON_RELEASE_DIR".Trim()
        $version = "$env:DAIMON_VERSION".Trim().TrimStart('v')
        if ($local) {
            $local = (Resolve-Path $local).Path
            $found = Get-ChildItem $local -Filter 'daimon-*-py3-none-any.whl' | Select-Object -First 1
            if (-not $found) { throw "No daimon-*.whl in $local." }
            $version = $found.Name -replace '^daimon-(.+)-py3-none-any\.whl$', '$1'
        }
        if (-not $version) {
            try {
                $latest = Invoke-RestMethod -UseBasicParsing -Uri "https://api.github.com/repos/$Repo/releases/latest"
                $version = "$($latest.tag_name)".TrimStart('v')
            } catch {
                $version = ''
            }
            if (-not $version) { throw "Couldn't find the latest release. Set `$env:DAIMON_VERSION = 'X.Y.Z' and try again." }
        }
        function Get-Wheel([string]$Name) {
            if ($local) { return (Join-Path $local "$Name-$version-py3-none-any.whl") }
            return "https://github.com/$Repo/releases/download/v$version/$Name-$version-py3-none-any.whl"
        }
        $wheel = Get-Wheel 'daimon'

        # Keep the add-ons an earlier install has (the wizard adds them), so upgrading doesn't drop them.
        $installArgs = @('tool', 'install', '--python', '3.12', '--force', $wheel)
        try {
            $py = Join-Path (& $uv tool dir | Out-String).Trim() 'daimon\Scripts\python.exe'
            foreach ($m in @('daimon_canvas', 'daimon_messages')) {
                if (Test-Path $py) {
                    & $py -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$m') else 1)"
                    if ($LASTEXITCODE -eq 0) {
                        $installArgs += @('--with', (Get-Wheel $m))
                    }
                }
            }
        } catch { }

        # A running Daimon (started at login, or the messages service setup started) keeps its files open and
        # Windows won't replace them, so stop it first and start it again afterwards.
        $restart = $false
        if (-not $DryRun) {
            $toolDir = Join-Path (& $uv tool dir | Out-String).Trim() 'daimon'
            $old = Get-Command daimon -ErrorAction SilentlyContinue
            if ($old) {
                try {
                    $restart = (cmd /c "`"$($old.Source)`" service status 2>nul" | Out-String) -match ', running'
                    if ($restart) {
                        Write-Host 'Stopping the running Daimon so it can be updated...'
                        & $old.Source service stop | Out-Null
                    }
                } catch { }
            }
            Get-CimInstance Win32_Process | Where-Object {
                ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($toolDir, [StringComparison]::OrdinalIgnoreCase)) -or
                ($_.CommandLine -and $_.CommandLine.IndexOf($toolDir, [StringComparison]::OrdinalIgnoreCase) -ge 0)
            } | ForEach-Object {
                Write-Host "Stopping $($_.Name) (pid $($_.ProcessId)), which is running from the old install..."
                cmd /c "taskkill /PID $($_.ProcessId) /T /F >nul 2>&1"
                Wait-Process -Id $_.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
            }
        }

        Write-Host "Installing Daimon $version..."
        for ($try = 1; ; $try++) {
            try { Invoke-Native $uv $installArgs; break }
            catch {
                if ($DryRun -or $try -ge 4) { throw }
                # uv sometimes can't delete the old install even with nothing running from it (a scanner or file
                # watcher holding the folder); PowerShell can. The add-ons to keep were already worked out above.
                Write-Host 'Clearing out the old install and trying again...'
                Start-Sleep -Seconds 2
                Remove-Item -Recurse -Force $toolDir -ErrorAction SilentlyContinue
            }
        }
        try { Invoke-Native $uv @('tool', 'update-shell') } catch { Write-Warning $_ }

        if (-not $DryRun) {
            $bin = (& $uv tool dir --bin | Out-String).Trim()
            if ($bin -and ($env:Path -split ';') -notcontains $bin) { $env:Path = "$bin;$env:Path" }
        }

        Write-Host ''
        Write-Host "Daimon $version is installed. If ``daimon`` isn't found later, open a new terminal." -ForegroundColor Green
        if ($env:DAIMON_NO_SETUP) {
            Write-Host 'Run `daimon setup` when you''re ready.'
        } else {
            Invoke-Native 'daimon' @('setup')
        }
        if ($restart) {
            try { Invoke-Native 'daimon' @('service', 'start') } catch { Write-Warning $_ }
        }
    } catch {
        Write-Host "daimon install: $($_.Exception.Message)" -ForegroundColor Red
    }
}
