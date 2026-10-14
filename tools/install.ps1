[CmdletBinding()]
param(
    [string]$PythonPath,
    [switch]$CheckOnly,
    [switch]$NoPause,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$projectDir = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$venvDir = Join-Path $projectDir '.venv'
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
$installDir = Join-Path $projectDir '.install'
$statusLog = Join-Path $installDir 'install.log'
$installLock = $null
$controlLock = $null
$workerLock = $null
$exitCode = 0

function Write-Stage([string]$Message) {
    Write-Host $Message
    if (Test-Path -LiteralPath $installDir) {
        Add-Content -LiteralPath $statusLog -Encoding UTF8 -Value (('[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message))
    }
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "A setup command failed (exit $LASTEXITCODE). Review the message above, then run install.cmd again."
    }
}

function Get-PythonInfo([string]$Executable, [string[]]$PrefixArguments = @()) {
    if (-not $Executable -or -not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }
    if ($Executable -match '[\\/]WindowsApps[\\/]') { return $null }
    $probe = @'
import json, sys, struct, sysconfig, ssl, sqlite3, venv, ensurepip
if sys.version_info < (3, 12) or struct.calcsize('P') != 8 or sysconfig.get_config_var('Py_GIL_DISABLED'):
    raise SystemExit(1)
print(json.dumps({'executable': sys.executable, 'version': sys.version.split()[0], 'prefix': sys.prefix, 'base': sys.base_prefix}))
'@
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $result = @(& $Executable @PrefixArguments -I -B -c $probe 2>$null)
        if ($LASTEXITCODE -eq 0 -and $result.Count -gt 0) {
            return ($result[-1] | ConvertFrom-Json -ErrorAction Stop)
        }
    } catch { } finally { $ErrorActionPreference = $savedPreference }
    return $null
}

function Find-Python {
    if ($PythonPath) {
        $info = Get-PythonInfo ([IO.Path]::GetFullPath($PythonPath))
        if (-not $info) { throw '-PythonPath must point to a working, standard 64-bit Python 3.12+ with venv and pip.' }
        return $info
    }
    $candidates = @()
    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) { $candidates += $command.Path }
    }
    foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore')) {
        if (Test-Path -LiteralPath $root) {
            foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
                $location = Get-Item -LiteralPath (Join-Path $key.PSPath 'InstallPath') -ErrorAction SilentlyContinue
                if ($location) {
                    $path = $location.GetValue('ExecutablePath')
                    if (-not $path) { $path = Join-Path $location.GetValue('') 'python.exe' }
                    $candidates += $path
                }
            }
        }
    }
    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        $info = Get-PythonInfo $candidate
        if ($info) { return $info }
    }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.13', '-3.12', '-3.14', '-3')) {
            $info = Get-PythonInfo $launcher.Path @($version)
            if ($info) { return $info }
        }
    }
    return $null
}

function Install-Python {
    # Official PSF files and SHA-256 from the Python 3.13.16 release page.
    $release = '3.13.16'
    $architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITEW6432')
    if (-not $architecture) { $architecture = [Environment]::GetEnvironmentVariable('PROCESSOR_ARCHITECTURE') }
    if ($architecture -eq 'ARM64') {
        $suffix = 'arm64'
        $checksum = '696e2226062c6ec3622c13f143d28336a856968518e49cb4f4c62513215a4f5d'
        $directoryName = 'Python313-arm64'
    } else {
        $suffix = 'amd64'
        $checksum = 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b'
        $directoryName = 'Python313'
    }
    $filename = "python-$release-$suffix.exe"
    $installer = Join-Path $installDir $filename
    $cached = (Test-Path -LiteralPath $installer) -and ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash -eq $checksum)
    if (-not $cached) {
        Write-Stage "No suitable Python found. Downloading official Python $release ($suffix)..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $download = "$installer.download"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/$release/$filename" -OutFile $download -TimeoutSec 120
        if ((Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $checksum) {
            throw 'Python download failed its SHA-256 check. It was not executed; retry the installation.'
        }
        Move-Item -LiteralPath $download -Destination $installer -Force
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Python Software Foundation(?:,|$)') {
        throw 'Python installer signature could not be verified. Check the Windows clock and certificate updates, then retry.'
    }
    $target = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) "Programs\Python\$directoryName"
    Write-Stage "Installing Python for the current Windows user: $target"
    $arguments = @('/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_pip=1', 'Include_launcher=0',
        'Include_test=0', 'Include_doc=0', 'Include_tcltk=0', 'Shortcuts=0', 'AssociateFiles=0', ('TargetDir="{0}"' -f $target))
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) { throw "Python installation failed (exit $($process.ExitCode))." }
    $info = Get-PythonInfo (Join-Path $target 'python.exe')
    if (-not $info) { throw 'Python is not usable yet. Restart Windows if requested by the installer, then rerun install.cmd.' }
    return $info
}

$configurationCode = @'
import json, sys
sys.path.insert(0, sys.argv[1])
from csvhub.config import Settings
settings = Settings.from_env()
print(json.dumps({'data_dir': str(settings.data_dir), 'host': settings.host, 'port': settings.port}))
'@

$guardCode = @'
import importlib.util, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
project = os.path.normcase(str(Path(sys.argv[1]).resolve()))
if importlib.util.find_spec('psutil'):
    import psutil
    for process in psutil.process_iter():
        if process.pid == os.getpid():
            continue
        service = False
        try:
            command = process.cmdline()
            service = any(command[i] == '-m' and command[i+1] in ('csvhub.api', 'csvhub.worker') for i in range(len(command)-1))
            if service and os.path.normcase(str(Path(process.cwd()).resolve())) == project:
                raise SystemExit('Project services are running. Run stop.cmd before installing or updating dependencies.')
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            if service:
                raise SystemExit('Cannot inspect a service. Run stop.cmd with its original privileges first.')
'@

$buildCode = @'
import json, sys, tomllib
from pathlib import Path
from setuptools.build_meta import build_editable
# Build in a shallow project directory to avoid pip's long Windows cache paths.
directory = Path(sys.argv[1]).resolve()
filename = build_editable(str(directory))
with Path('pyproject.toml').open('rb') as file:
    dependencies = tomllib.load(file)['project']['dependencies']
print(json.dumps({'wheel': str(directory / filename), 'dependencies': dependencies}))
'@

$verifyCode = @'
import json, sqlite3, sys
from pathlib import Path
import fastapi, uvicorn, openpyxl, xlrd, defusedxml, psutil, pydantic
import csvhub.api, csvhub.worker
from csvhub import __version__
from csvhub.config import Settings
if Path(csvhub.__file__).resolve().parents[1] != Path(sys.argv[1]).resolve():
    raise SystemExit('The environment points to another project. Run install.cmd again.')
if sqlite3.sqlite_version_info < (3, 37, 0):
    raise SystemExit('SQLite 3.37+ is required; choose a newer Python installation.')
with sqlite3.connect(':memory:') as db:
    db.execute('CREATE TABLE installation_check (value TEXT) STRICT')
    if db.execute('SELECT json_valid(?)', (json.dumps({'ready': True}),)).fetchone()[0] != 1:
        raise SystemExit('The bundled SQLite JSON functions are not usable.')
settings = Settings.from_env()
if sys.argv[2] == 'prepare':
    settings.prepare()
    settings.token()
print('CSVHub ' + __version__ + '; Python ' + sys.version.split()[0] + '; SQLite ' + sqlite3.sqlite_version)
print('Data directory: ' + str(settings.data_dir))
print('Listening configuration: ' + settings.host + ':' + str(settings.port))
'@

if ($Help) {
    Write-Host 'CSVHub one-click Windows installation'
    Write-Host 'Usage: install.cmd [-PythonPath "C:\Python313\python.exe"] [-NoPause] [-CheckOnly]'
    Write-Host 'Requires 64-bit Windows 10 / Server 2016 or newer and Internet access for installation.'
    Write-Host 'Creates .venv, installs runtime dependencies, preserves .env and existing data.'
    Write-Host '-CheckOnly validates an existing installation without downloads or changes.'
    exit 0
}

try {
    Set-Location -LiteralPath $projectDir
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Text.UTF8Encoding]::new($false)
    if (-not [Environment]::Is64BitOperatingSystem -or [Environment]::OSVersion.Version.Major -lt 10) {
        throw '64-bit Windows 10 / Server 2016 or newer is required.'
    }
    # Isolate the project from unrelated Python environment variables and user packages.
    $env:PYTHONHOME = $null
    $env:PYTHONPATH = $null
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'

    if ($CheckOnly) {
        if (-not (Get-PythonInfo $venvPython)) { throw 'No working project environment was found. Run install.cmd first.' }
        Invoke-Checked $venvPython @('-I', '-B', '-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', 'check')
        Invoke-Checked $venvPython @('-I', '-B', '-X', 'utf8', '-c', $verifyCode, $projectDir, 'check')
        Write-Host 'Installation check passed.'
    } else {
        [IO.Directory]::CreateDirectory($installDir) | Out-Null
        try { $installLock = [IO.File]::Open((Join-Path $installDir 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { throw 'Another installation is running. Wait for it to finish, then retry.' }
        $temporaryDir = Join-Path $installDir 'tmp'
        [IO.Directory]::CreateDirectory($temporaryDir) | Out-Null
        $env:TEMP = $temporaryDir
        $env:TMP = $temporaryDir
        Write-Stage '[1/6] Checking Python...'
        if ((Test-Path -LiteralPath $venvDir) -and ((Get-Item -LiteralPath $venvDir).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw '.venv is a directory link. Use a normal project directory before running this installer.'
        }
        $existingInfo = Get-PythonInfo $venvPython
        if ($existingInfo -and [IO.Path]::GetFullPath($existingInfo.prefix) -ne $venvDir) { $existingInfo = $null }
        if ($existingInfo -and -not $PythonPath) { $pythonInfo = $existingInfo }
        else { $pythonInfo = Find-Python }
        if (-not $pythonInfo) { $pythonInfo = Install-Python }
        Write-Stage ("Using Python {0}: {1}" -f $pythonInfo.version, $pythonInfo.executable)

        Write-Stage '[2/6] Preparing project configuration...'
        $configuration = Join-Path $projectDir '.env'
        if (-not (Test-Path -LiteralPath $configuration)) {
            $example = [IO.File]::ReadAllBytes((Join-Path $projectDir '.env.example'))
            $file = [IO.File]::Open($configuration, 'CreateNew', 'Write', 'None')
            try { $file.Write($example, 0, $example.Length) } finally { $file.Dispose() }
            Write-Stage 'Created .env with default settings.'
        } else { Write-Stage 'Keeping existing .env settings.' }
        $configurationOutput = @(& $pythonInfo.executable -c $configurationCode $projectDir)
        if ($LASTEXITCODE -ne 0) { throw 'Project configuration is invalid. Correct .env or the CSVHUB_* environment variables, then retry.' }
        $settings = $configurationOutput[-1] | ConvertFrom-Json
        if ($existingInfo) { Invoke-Checked $venvPython @('-c', $guardCode, $projectDir) }
        [IO.Directory]::CreateDirectory($settings.data_dir) | Out-Null
        try {
            $controlLock = [IO.File]::Open((Join-Path $settings.data_dir 'service-control.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
            $workerLock = [IO.File]::Open((Join-Path $settings.data_dir 'worker.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        } catch { throw 'The project is busy. Run stop.cmd with its original privileges before installing.' }

        Write-Stage '[3/6] Preparing the project virtual environment...'
        if ($PythonPath -and $existingInfo -and $pythonInfo.base -ne $existingInfo.base) { $existingInfo = $null }
        if (-not $existingInfo) {
            if (Test-Path -LiteralPath $venvDir) {
                $backup = Join-Path $projectDir ('.venv.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
                if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($backup)) -ne $projectDir -or [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($venvDir)) -ne $projectDir) {
                    throw 'Environment backup paths are outside the project.'
                }
                Move-Item -LiteralPath $venvDir -Destination $backup
                Write-Stage 'Preserved the previous environment in a .venv.backup-* directory.'
            }
            Invoke-Checked $pythonInfo.executable @('-I', '-X', 'utf8', '-m', 'venv', $venvDir)
        } else { Write-Stage 'Reusing the existing project virtual environment.' }
        if (-not (Get-PythonInfo $venvPython)) { throw 'Virtual environment creation failed.' }
        Invoke-Checked $venvPython @('-I', '-X', 'utf8', '-m', 'ensurepip', '--upgrade')

        Write-Stage '[4/6] Installing Python packaging tools and runtime dependencies...'
        $pipOptions = @('-I', '-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', 'install', '--no-input', '--timeout', '30', '--retries', '2', '--only-binary=:all:')
        Invoke-Checked $venvPython ($pipOptions + @('pip>=24', 'setuptools>=75', 'wheel>=0.44'))
        $wheelDirectory = Join-Path $installDir 'wheels'
        [IO.Directory]::CreateDirectory($wheelDirectory) | Out-Null
        $buildOutput = @(& $venvPython -I -X utf8 -c $buildCode $wheelDirectory)
        if ($LASTEXITCODE -ne 0) {
            $buildOutput | ForEach-Object { Write-Host $_ }
            throw 'Project package preparation failed. Review the message above and retry.'
        }
        $buildDetails = $buildOutput[-1] | ConvertFrom-Json
        # Read only the runtime requirements declared by this project's metadata.
        Invoke-Checked $venvPython ($pipOptions + @($buildDetails.dependencies))
        Invoke-Checked $venvPython ($pipOptions + @('--force-reinstall', '--no-deps', $buildDetails.wheel))

        Write-Stage '[5/6] Checking dependencies, SQLite and project imports...'
        Invoke-Checked $venvPython @('-I', '-X', 'utf8', '-m', 'pip', '--disable-pip-version-check', 'check')
        Invoke-Checked $venvPython @('-I', '-X', 'utf8', '-c', $verifyCode, $projectDir, 'prepare')
        Write-Stage '[6/6] Installation completed.'
        Write-Host ''
        Write-Host 'Next: double-click start.cmd to start CSVHub and open the login page.'
        Write-Host 'Service scripts: start.cmd / restart.cmd / stop.cmd'
        Write-Host 'Settings: .env (system CSVHUB_* variables take priority).'
        Write-Host 'First administrator login: use the administrator token in the data directory, then create your account.'
        Write-Host 'Existing data, accounts and administrator tokens are preserved.'
    }
} catch {
    $exitCode = 1
    Write-Host ''
    Write-Host ("Installation failed: " + $_.Exception.Message) -ForegroundColor Red
    if (-not $CheckOnly -and (Test-Path -LiteralPath $statusLog)) {
        Add-Content -LiteralPath $statusLog -Encoding UTF8 -Value (('[{0}] FAILED: {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $_.Exception.Message))
        Write-Host "Progress log: $statusLog"
    }
} finally {
    if ($workerLock) { $workerLock.Dispose() }
    if ($controlLock) { $controlLock.Dispose() }
    if ($installLock) { $installLock.Dispose() }
    if (-not $NoPause) {
        try { [void](Read-Host 'Press Enter to close') } catch { }
    }
}
exit $exitCode
