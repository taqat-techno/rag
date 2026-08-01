<#
.SYNOPSIS
    Prove that an existing RAG Tools installation can be replaced, BEFORE Setup
    writes anything - or refuse, having changed nothing.

.DESCRIPTION
    The failure this exists to make impossible, reproduced on a GitHub-hosted
    windows-latest runner (job 91273392455), genuine packaged v3.3.0 -> candidate:

        >>> installing v3.3.0 ... exit=0
          [PASS] previous release is serving - managed engine pid 1264
          [PASS] previous release has live processes holding its binaries - 3
        >>> installing the candidate over it ... exit=5
        --- Inno log ... (last 120 of 1 lines) ---   <- Setup died before its log
          [FAIL] rag.exe reports the new version - [PYI-2072] Failed to load
                 Python DLL '...\Programs\RAGTools\_internal\python312.dll'
          [FAIL] uninstall registry entry updated - registry reads 3.3.0
          [FAIL] the service answers after the upgrade - nothing within 300s

    Mechanism: CloseApplications=no, so Restart Manager is not in play;
    CurStepChanged(ssInstall) stopped and killed; [InstallDelete] then removed
    {app}\_internal wholesale, and THAT is the first destructive write. Anything
    still holding _internal\python312.dll turns that delete into Inno's
    file-in-use Abort/Retry/Ignore box, /SUPPRESSMSGBOXES answers Abort, and
    Setup exits 5 with _internal half deleted.

    So the defect is not the kill. It is that the installer started deleting
    before it had proven the files were replaceable, and had no way to refuse.

    THE INVARIANT:
        If any installation-owning process cannot be stopped, the upgrade aborts
        BEFORE its first destructive write and the old installation remains
        runnable.

    PowerShell, not a bundled executable, because PowerShell is present on every
    Windows and this has to run before anything is installed.

    NOTHING HERE DELETES OR OVERWRITES ANYTHING UNDER -AppDir. That is not a
    convention, it is the acceptance criterion: a refusal must leave the old
    installation byte-consistent and runnable, and the cheapest way to guarantee
    that is for the refusing code to contain no destructive operation at all.
    The only files this writes are its own log, its blocker summary and its task
    record - all of them under -LogPath's directory.

.PARAMETER AppDir
    The installation directory being replaced ({app}).

.PARAMETER DataDir
    The product's data root (%LOCALAPPDATA%\RAGTools). Owned processes may run
    from here too - the managed Qdrant engine does.

.PARAMETER LogPath
    Where the structured JSON verdict is written. Its directory also receives
    the human-readable blocker summary and the scheduled-task record.

.PARAMETER TimeoutSeconds
    Hard ceiling on the whole protocol.

.PARAMETER Mode
    Quiesce (default) - the pre-install protocol.
    Restore           - re-enable exactly the scheduled tasks Quiesce disabled.

.NOTES
    Exit codes - see $Script:ExitCodes below:
        0  quiescent, proceed
        2  could not prove quiescence - abort, nothing was modified
        3  internal error - treat as abort
    1 is deliberately unused: an unhandled PowerShell error exits 1, and that
    must not be mistakable for a considered verdict.

    The decision policy is mirrored, testably, in
    src/ragtools/upgrade/quiescence.py. The phase list and the exit-code mapping
    are declared exactly once on each side and compared structurally by
    tests/test_installer_quiescence_contract.py, because two hand-maintained
    lists are not a single source of truth.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $AppDir,
    [Parameter(Mandatory = $true)] [string] $DataDir,
    [Parameter(Mandatory = $true)] [string] $LogPath,
    [int] $TimeoutSeconds = 120,
    [ValidateSet('Quiesce', 'Restore')] [string] $Mode = 'Quiesce'
)

# Deliberately NOT Set-StrictMode. This script's worst outcome is a FALSE
# refusal: exit 3 on every machine because a property was absent on one Windows
# build. An upgrade that cannot proceed is worse than one that proceeds without
# a nicety, so every access here is guarded by hand instead.
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# The protocol, named once. Mirrors ragtools.upgrade.quiescence.PHASES.
# ---------------------------------------------------------------------------

$Script:Phases = @(
    'detect_installation',
    'enter_maintenance',
    'disable_tasks',
    'stop_tray',
    'stop_supervisor',
    'stop_service_children',
    'stop_managed_engine',
    'identify_holders',
    'wait_for_exit',
    'retry_safe_shutdown',
    'verify_clear',
    'probe_replaceable'
)

# The one phase that runs AFTER the install, not before it.
$Script:RestorePhase = 'restore_task_state'

# Phases whose shutdown request may be issued again during the retry loop.
# 'disable_tasks' is DELIBERATELY absent: a second pass would read the prior
# state as Disabled - because the first pass disabled it - and overwrite the
# only evidence of what to restore. A retry that destroys the rollback
# information is a destructive step wearing a safe name.
$Script:RetryablePhases = @(
    'stop_tray',
    'stop_supervisor',
    'stop_service_children',
    'stop_managed_engine'
)

# Phases whose FAILURE could let this script wrongly conclude quiescence, and
# which therefore abort the protocol. Everything else is ADVISORY: an advisory
# phase that fails is recorded and the protocol carries on, because the decisive
# phases still have to pass before anything proceeds.
#
#   detect_installation  decides whether there is anything to protect at all. An
#                        error read as "nothing installed" returns exit 0 and
#                        lets Setup delete a LIVE installation.
#   verify_clear         is one of the two that actually prove the answer.
#   probe_replaceable    is the other, and the stronger of the two.
#
# This distinction is not decoration; it is the 3.5.1 defect. `disable_tasks`
# threw on a scheduled task that was merely not registered, `Invoke-Phase`
# rethrew, and the protocol aborted with `internal_error` BEFORE stop_tray -
# so no tray stop, no supervisor stop, no taskkill and no engine stop ever ran.
# All five upgrade legs refused with the previous version still serving and
# every one of its processes alive, and the refusal named an exception instead
# of a blocker. One preparatory step failing must never be able to skip every
# step that does the actual work.
$Script:DecisivePhases = @(
    'detect_installation',
    'verify_clear',
    'probe_replaceable'
)

$Script:ExitCodes = @{
    quiescent      = 0
    not_quiescent  = 2
    internal_error = 3
}

$Script:Schema = 'ragtools.upgrade.quiescence/1'

# TWO MECHANISMS, because the two problems are different. Carried across from
# installer.iss, where it was learned the expensive way and must not be lost:
#
#   rag.exe and ragw.exe are names this product owns outright - nothing else on
#   a Windows machine ships them - so matching by image name is both safe and,
#   decisively, PROVEN: v3.0.1 replaced a running rag.exe in place on a real
#   machine using exactly this call. A path-scoped PowerShell equivalent was
#   tried in its place and the upgrade failed with `DeleteFile failed; code 5`,
#   with owned processes still alive afterwards. A mechanism that works is not
#   replaced by a tidier one that does not.
#
#   qdrant.exe is the opposite case and keeps the scoping: it is a common name,
#   and storage_backend = "external" explicitly means "a server you run
#   yourself". Killing every Qdrant on the machine would be data loss in someone
#   else's application caused by installing this one.
$Script:OwnedImageNames      = @('rag.exe', 'ragw.exe')
$Script:PathScopedImageNames = @('qdrant.exe')

# Get-Process -Name matches ProcessName, which carries no extension. '-Name
# qdrant.exe' matches nothing AND reports no error, so a kill built on it would
# silently spare the process it was aiming at.
$Script:OwnedProcessNames = @('rag', 'ragw', 'qdrant')

# File kinds Windows keeps mapped and therefore refuses to replace while a
# handle survives. These are what [InstallDelete] and [Files] are about to
# touch, so these are what must be proven replaceable first.
$Script:ReplaceableSuffixes = @('.exe', '.dll', '.pyd')

# Scheduled tasks an installation owns. The third is v2's, still present on
# every machine that ever ran one.
$Script:OwnedTasks = @('\RAGTools\Service', '\RAGTools\Tray', '\RAGTools Watchdog')

$Script:SettleMilliseconds = 2500
# Bounded independently of -TimeoutSeconds, so "this process is refusing to die"
# and "the whole protocol ran out of time" stay distinguishable. They have
# different remedies and the user is told which one happened.
$Script:SettleRounds = 3
$Script:RetryBackoffSeconds = @(1, 3, 9)

# How the engine stop AWAITS. `Stop-Process` returns when the request has been
# issued; the kernel releases the image's file handle afterwards, and the delete
# that follows fails in exactly that gap. So the phase does not report until it
# has LOOKED and found the process gone.
$Script:PollMilliseconds        = 250
$Script:EngineGraceMilliseconds = 2000
$Script:EngineExitMilliseconds  = 10000

# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

$Script:PhaseRecords = New-Object System.Collections.ArrayList
$Script:Blockers     = New-Object System.Collections.ArrayList
$Script:TaskRecords  = New-Object System.Collections.ArrayList
$Script:Started      = [System.Diagnostics.Stopwatch]::StartNew()
$Script:TimedOut     = $false
$Script:Installation = @{ present = $false; version = ''; executable = '' }

function Get-DeadlineExceeded {
    return ($Script:Started.Elapsed.TotalSeconds -ge $TimeoutSeconds)
}

function Add-Blocker {
    param([string] $Kind, [string] $Identity, [string] $Detail,
          $OwningProcessId = $null, [string] $Path = $null)
    $blocker = [ordered]@{
        kind     = $Kind
        identity = $Identity
        detail   = $Detail
        pid      = $OwningProcessId
        path     = $Path
    }
    [void] $Script:Blockers.Add($blocker)
    return $blocker
}

# Every phase goes through here. One dispatcher means the declared list above
# and the executed list cannot drift, and the contract test asserts exactly
# that by comparing the -Name arguments against $Script:Phases.
function Invoke-Phase {
    param(
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [scriptblock] $Body
    )
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $outcome = 'ok'
    $detail = ''
    $found = New-Object System.Collections.ArrayList
    try {
        # Last, not only: a phase body that emits an incidental object must not
        # turn the verdict into an array and the array into an internal error.
        $result = @(& $Body) | Where-Object { $_ -is [System.Collections.IDictionary] } |
                  Select-Object -Last 1
        if ($null -ne $result) {
            if ($result.Contains('outcome')) { $outcome = [string] $result['outcome'] }
            if ($result.Contains('detail'))  { $detail  = [string] $result['detail'] }
            if ($result.Contains('blockers')) {
                foreach ($b in @($result['blockers'])) { [void] $found.Add($b) }
            }
        }
    }
    catch {
        $outcome = 'error'
        $detail = "$($_.Exception.GetType().Name): $($_.Exception.Message)"
        $watch.Stop()
        [void] $Script:PhaseRecords.Add([ordered]@{
            phase = $Name; outcome = $outcome; detail = $detail
            duration_ms = [int] $watch.Elapsed.TotalMilliseconds; blockers = @()
        })
        # A DECISIVE phase failing means the answer is unknown, and unknown
        # refuses. An ADVISORY phase failing means one attempt to help did not
        # work - which proves nothing either way, and must not skip the phases
        # that do the stopping and the proving. The record above keeps the
        # failure visible in the verdict regardless.
        if ($Script:DecisivePhases -contains $Name) { throw }
        return
    }
    $watch.Stop()
    [void] $Script:PhaseRecords.Add([ordered]@{
        phase       = $Name
        outcome     = $outcome
        detail      = $detail
        duration_ms = [int] $watch.Elapsed.TotalMilliseconds
        blockers    = @($found)
    })
}

function Add-SkippedPhase {
    param([string] $Name, [string] $Detail)
    [void] $Script:PhaseRecords.Add([ordered]@{
        phase = $Name; outcome = 'skipped'; detail = $Detail
        duration_ms = 0; blockers = @()
    })
}

# ---------------------------------------------------------------------------
# Every external process this script starts is BOUNDED.
#
# Inno waits for this script, and this script runs the PREVIOUS release's
# rag.exe - any version ever published, whose behaviour when its own service is
# wedged is not knowable from here. Unbounded, one old binary that never returns
# stops the upgrade dead with no output explaining why. Observed: a silent
# upgrade over a running v2.7.0 produced no output and did not finish in forty
# minutes.
# ---------------------------------------------------------------------------

function Invoke-Bounded {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [string[]] $ArgumentList = @(),
        [int] $TimeoutMilliseconds = 60000
    )
    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList `
                              -WindowStyle Hidden -PassThru -ErrorAction Stop
    }
    catch { return -1 }
    if ($null -eq $proc) { return -1 }
    if (-not $proc.WaitForExit($TimeoutMilliseconds)) {
        try { $proc.Kill() } catch { }
        return 258      # WAIT_TIMEOUT, and never mistaken for a real exit code
    }
    return $proc.ExitCode
}

function Invoke-Native {
    <#
        Run a native command and READ ITS OUTPUT, without its stderr being able
        to abort this script.

        THIS IS THE 3.5.1 DEFECT, and it is subtle enough to deserve the whole
        comment. In Windows PowerShell, output a native command writes to stderr
        is turned into an ErrorRecord as soon as a redirection operator is
        applied to stream 2 - and with $ErrorActionPreference = 'Stop', that
        ErrorRecord is TERMINATING. `schtasks /query` writes

            ERROR: The system cannot find the file specified.

        to stderr for a task that is simply not registered, which is the normal
        case for at least one of $Script:OwnedTasks on EVERY release this
        upgrades from (`\RAGTools Watchdog` is v2's and v3 removes it; a v2
        machine has neither `\RAGTools\Service` nor `\RAGTools\Tray`). So the
        `2>$null` that was there to keep the console quiet turned "this task is
        not registered" into a RemoteException, `Get-TaskState` never returned
        'Missing', and disable_tasks aborted the protocol before a single stop
        phase ran. Reproduced exactly: five upgrade legs, every one exit 7, the
        previous version still serving with all of its processes alive.

        The preference is neutralised HERE, around the call and nowhere else -
        every other statement in this script still wants Stop. Both streams are
        captured so a diagnostic can quote what the command actually said, and a
        launch failure is reported rather than thrown.
    #>
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [string[]] $ArgumentList = @()
    )
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    $lines = New-Object System.Collections.ArrayList
    try {
        foreach ($item in @(& $FilePath @ArgumentList 2>&1)) {
            [void] $lines.Add([string] $item)
        }
    }
    catch {
        return @{ ExitCode = -1; Lines = @(); Failed = $true
                  Detail = "$($_.Exception.GetType().Name): $($_.Exception.Message)" }
    }
    return @{ ExitCode = $LASTEXITCODE; Lines = @($lines); Failed = $false; Detail = '' }
}

# ---------------------------------------------------------------------------
# Ownership - the decision that must never be made by image name alone
# ---------------------------------------------------------------------------

function Get-NormalizedPath {
    param([string] $Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    return ($Path -replace '\\', '/').TrimEnd('/').ToLowerInvariant()
}

function Test-UnderRoot {
    param([string] $Path, [string] $Root)
    $child  = Get-NormalizedPath $Path
    $parent = Get-NormalizedPath $Root
    if (-not $child -or -not $parent) { return $false }
    # The boundary is a separator, so C:/Programs/RAGTools-old is NOT under
    # C:/Programs/RAGTools. A bare prefix comparison is how a neighbouring
    # directory gets swept into someone else's upgrade.
    return ($child -eq $parent) -or ($child.StartsWith($parent + '/'))
}

function Get-ProcessTable {
    <#
        pid -> executable path and command line, asked of the WMI service.

        THIS IS THE 3.5.1 ENGINE DEFECT, and nothing about it is visible in the
        code that broke. `installer.iss` declares no 64-bit architecture, so
        Inno's Setup binary is 32-bit and `Exec('powershell.exe', ...)` resolves
        through WOW64 to the 32-bit PowerShell. From a 32-bit host, .NET's
        Process.Path and Process.Modules go through EnumProcessModules against a
        64-bit target. Measured on Windows 11, 32-bit PowerShell, live product:

            pid 46132 qdrant: Path=<NULL> (no exception) Modules=[]
            pid 18732 rag   : Path=<NULL> (no exception) Modules=[]

        NOT an error. $null and an EMPTY collection, silently, for every 64-bit
        process on the machine - which is every process this product runs. So
        Get-OwnedProcess collected no executable evidence and no module
        evidence, the `catch` never fired, and qdrant.exe - which earns no
        image-name evidence BY DESIGN, because the name is common and
        storage_backend = "external" means a server the user runs - produced no
        evidence at all. The managed engine was therefore invisible to
        stop_managed_engine, to identify_holders, to wait_for_exit and to
        verify_clear, which reported "no owned process is running" while the
        engine held four files [InstallDelete] was about to remove. Only
        probe_replaceable saw the truth, and by then all it can do is refuse:

            reason=files_not_replaceable  quiescent=False  elapsed=17.126s
            blocker: <install>\bin\qdrant.exe          in use by another process
            blocker: <install>\_internal\msvcp140.dll          "
            blocker: <install>\_internal\vcruntime140.dll      "
            blocker: <install>\_internal\vcruntime140_1.dll    "

        with ZERO process blockers, and an elapsed far too short for the retry
        loop to have run - because there was never anything for it to retry.
        rag.exe and ragw.exe survived the same blindness only because they are
        killed by NAME through taskkill, which needs no introspection. That also
        explains, at last, the measurement recorded above this file's image-name
        lists: the path-scoped PowerShell equivalent that "failed with
        DeleteFile failed; code 5, with owned processes still alive" failed for
        exactly this reason.

        Win32_Process is answered by the WMI service out of process, so it is
        bitness-independent and reads no target memory. From the SAME 32-bit
        host, the same pid, it returned the executable path and the command line
        correctly. One bulk query per enumeration (~450 ms for ~590 processes
        here), not one per process.

        A query that fails returns an EMPTY table rather than throwing: this is
        reached from decisive phases, and an exception here would abort the
        protocol instead of degrading its evidence.
    #>
    $table = @{}
    try {
        foreach ($row in @(Get-CimInstance -ClassName Win32_Process `
                                           -Property ProcessId, ExecutablePath, CommandLine `
                                           -ErrorAction Stop)) {
            $table[[int] $row.ProcessId] = @{
                ExecutablePath = [string] $row.ExecutablePath
                CommandLine    = [string] $row.CommandLine
            }
        }
    }
    catch { }
    return $table
}

function Get-OwnedProcess {
    <#
        Every process this installation owns, with the EVIDENCE that says so.

        Three kinds, deliberately asymmetric:
          * its executable lives under -AppDir or -DataDir;
          * it has MAPPED a module out of -AppDir - the case an image-name
            filter cannot see, and the case that produced [PYI-2072];
          * its image name is one this product owns outright.

        qdrant.exe earns no name evidence. That is the whole point - and it is
        why the executable path must be obtainable by more than one mechanism.
        For the engine the PATH IS THE OWNERSHIP PROOF, so one unreadable
        property is a single point of failure for the entire engine phase. See
        Get-ProcessTable for what that cost.

        A process whose modules cannot be read is RECORDED as unscannable rather
        than dropped. It is not, on its own, a blocker - or every upgrade would
        refuse on System - but it is not evidence of quiescence either. The
        replaceability probe is what decides; this is what explains.
    #>
    $table = Get-ProcessTable
    $owned = New-Object System.Collections.ArrayList
    foreach ($proc in @(Get-Process -ErrorAction SilentlyContinue)) {
        $evidence = New-Object System.Collections.ArrayList
        $exe = ''
        try { $exe = [string] $proc.Path } catch { $exe = '' }
        $commandLine = ''
        $row = $table[[int] $proc.Id]
        if ($null -ne $row) {
            # The fallback the engine's ownership rests on. This way round, not
            # the other: Process.Path is already in hand when it works.
            if (-not $exe) { $exe = [string] $row['ExecutablePath'] }
            $commandLine = [string] $row['CommandLine']
        }
        $modulesReadable = $true
        $modulePaths = @()
        try {
            $modulePaths = @($proc.Modules | ForEach-Object { $_.FileName })
        }
        catch {
            $modulesReadable = $false
            $modulePaths = @()
        }
        # An EMPTY module list is not a successful scan. A live process has at
        # least its own image mapped, so zero modules means the scan was refused
        # or could not cross the bitness boundary - and calling that a clean scan
        # is what let the 3.5.1 verdict report "0 process(es) refused a module
        # scan" from a host that could read none of them.
        if (@($modulePaths).Count -eq 0) { $modulesReadable = $false }

        if ((Test-UnderRoot $exe $AppDir) -or (Test-UnderRoot $exe $DataDir)) {
            [void] $evidence.Add('executable_under_installation')
        }
        foreach ($module in $modulePaths) {
            if (Test-UnderRoot $module $AppDir) {
                [void] $evidence.Add('loaded_module_under_installation')
                break
            }
        }
        $imageName = ''
        if ($exe) { $imageName = [System.IO.Path]::GetFileName($exe).ToLowerInvariant() }
        else { $imageName = ($proc.ProcessName + '.exe').ToLowerInvariant() }
        if ($Script:OwnedImageNames -contains $imageName) {
            [void] $evidence.Add('owned_image_name')
        }

        if ($evidence.Count -gt 0) {
            [void] $owned.Add([pscustomobject]@{
                Pid             = $proc.Id
                Name            = $imageName
                Executable      = $exe
                Evidence        = @($evidence)
                ModulesReadable = $modulesReadable
                Role            = (Get-ProcessRole -ImageName $imageName -CommandLine $commandLine)
            })
        }
    }
    return @($owned)
}

function Get-ProcessRole {
    param([string] $ImageName, [string] $CommandLine = '')
    if ($Script:PathScopedImageNames -contains $ImageName) { return 'engine' }
    if ($ImageName -like 'qdrant*') { return 'engine' }
    $command = ([string] $CommandLine).ToLowerInvariant()
    if ($command -match 'supervis') { return 'supervisor' }
    if ($command -match '(?<![a-z])tray(?![a-z])') { return 'tray' }
    if ($Script:OwnedImageNames -contains $ImageName) { return 'service' }
    return 'module_holder'
}

function Format-ProcessBlocker {
    param($Process)
    $detail = "$($Process.Role); $([string]::Join(', ', $Process.Evidence))"
    if ($Process.Executable) { $detail += "; $($Process.Executable)" }
    if (-not $Process.ModulesReadable) { $detail += '; loaded modules could not be read' }
    return (Add-Blocker -Kind 'process' -Identity "pid $($Process.Pid) $($Process.Name)" `
                        -Detail $detail -OwningProcessId $Process.Pid `
                        -Path $Process.Executable)
}

# ---------------------------------------------------------------------------
# Stopping - ask, then insist, and never conclude
# ---------------------------------------------------------------------------

function Request-GracefulStop {
    param([string] $Role)
    $exe = Join-Path $AppDir 'rag.exe'
    if (-not (Test-Path -LiteralPath $exe)) { return $false }
    switch ($Role) {
        'tray'             { return ((Invoke-Bounded -FilePath $exe -ArgumentList @('tray', 'stop') -TimeoutMilliseconds 30000) -eq 0) }
        'supervisor'       { return ((Invoke-Bounded -FilePath $exe -ArgumentList @('service', 'stop') -TimeoutMilliseconds 60000) -eq 0) }
        'service'          { return ((Invoke-Bounded -FilePath $exe -ArgumentList @('service', 'stop') -TimeoutMilliseconds 60000) -eq 0) }
        # The engine has no CLI stop of its own, and "stopping the service stops
        # it" is an assumption this product has already been bitten by: an engine
        # ORPHANED from a service that died (the 3.4.0 incident) has no parent
        # left to ask, and taskkill /T cannot reach it either. It is asked
        # directly in Stop-EngineProcess instead.
        'engine'           { return $false }
        default            { return $false }
    }
}

function Wait-ProcessGone {
    <#
        Await ACTUAL EXIT, by LOOKING - never by believing the request.

        Stop-Process returns once the request has been issued; the kernel
        releases the image's file handle afterwards, and the delete that follows
        fails in exactly that gap. Polling Get-Process rather than WaitForExit
        deliberately: WaitForExit needs a handle this script may not be able to
        open for a process it did not start, and a permission failure there would
        read as "it exited".

        Bounded twice - by its own timeout AND by the protocol deadline - so a
        process that will never die cannot consume the whole upgrade window.
    #>
    param([int] $ProcessId, [int] $TimeoutMilliseconds)
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        try { [void] (Get-Process -Id $ProcessId -ErrorAction Stop) }
        catch { return $true }
        if ($watch.Elapsed.TotalMilliseconds -ge $TimeoutMilliseconds) { return $false }
        if (Get-DeadlineExceeded) { return $false }
        Start-Sleep -Milliseconds $Script:PollMilliseconds
    }
}

function Stop-EngineProcess {
    <#
        Ask, then insist, then WAIT - in that order, for one managed engine.

        The caller has ALREADY proven this pid's executable lies under -AppDir or
        -DataDir. Nothing in here re-decides ownership and nothing in here may be
        reached by path, only by pid: stopping a Qdrant this installation does not
        own is data loss in someone else's application.

        Returns whether the process is gone. Not whether a request was accepted -
        that is the distinction the whole protocol is built on.
    #>
    param([int] $ProcessId)
    $proc = $null
    try { $proc = Get-Process -Id $ProcessId -ErrorAction Stop }
    catch { return $true }
    # Graceful first. qdrant.exe is a console process, so this lands only when it
    # has a window; when it has none the call is a no-op and the wait below falls
    # straight through to the terminate. Cheap either way, and it is the only
    # chance the engine gets to close its storage cleanly.
    try { [void] $proc.CloseMainWindow() } catch { }
    if (Wait-ProcessGone -ProcessId $ProcessId `
                         -TimeoutMilliseconds $Script:EngineGraceMilliseconds) {
        return $true
    }
    try { Stop-Process -Id $ProcessId -Force -ErrorAction Stop } catch { }
    return (Wait-ProcessGone -ProcessId $ProcessId `
                             -TimeoutMilliseconds $Script:EngineExitMilliseconds)
}

function Invoke-RoleStop {
    <#
        Issue stops for one role. Returns @{ asked; stopped; spared }.

        rag.exe / ragw.exe go through taskkill /F /IM ... /T - the PROVEN
        mechanism, deliberately not replaced by a tidier path-scoped one that
        was measured to fail. Everything else is stopped by PID, and the engine
        only when its executable lies under -AppDir or -DataDir.

        The engine additionally WAITS. It is the one role with no CLI stop and no
        taskkill behind it, so if this phase does not confirm the exit, nothing
        before probe_replaceable does - and probe_replaceable can only refuse.
    #>
    param([string] $Role)
    $asked = Request-GracefulStop -Role $Role
    $stopped = New-Object System.Collections.ArrayList
    $spared = 0
    $exited = 0

    if ($Role -eq 'service') {
        foreach ($image in $Script:OwnedImageNames) {
            [void] (Invoke-Bounded -FilePath "$env:SystemRoot\System32\taskkill.exe" `
                                   -ArgumentList @('/F', '/IM', $image, '/T') `
                                   -TimeoutMilliseconds 30000)
        }
    }

    foreach ($proc in (Get-OwnedProcess)) {
        if ($proc.Role -ne $Role) { continue }
        if ($Role -eq 'engine') {
            # The scoping rule, unchanged and load-bearing. An engine whose
            # executable we could not resolve AT ALL lands here too, and is
            # spared: unattributable is not ours.
            if (-not ((Test-UnderRoot $proc.Executable $AppDir) -or
                      (Test-UnderRoot $proc.Executable $DataDir))) {
                $spared++
                continue
            }
            [void] $stopped.Add($proc.Pid)
            if (Stop-EngineProcess -ProcessId $proc.Pid) { $exited++ }
            continue
        }
        try {
            Stop-Process -Id $proc.Pid -Force -ErrorAction Stop
            [void] $stopped.Add($proc.Pid)
        }
        catch { }
    }
    return @{ asked = $asked; stopped = @($stopped); spared = $spared; exited = $exited }
}

function Format-StopDetail {
    param($Result, [string] $Role)
    $detail = "graceful request $(if ($Result.asked) { 'accepted' } else { 'unavailable' }); " +
              "stop issued to $(@($Result.stopped).Count) process(es)"
    if ($Role -eq 'engine') {
        $issued = @($Result.stopped).Count
        # Named separately from the count of stops ISSUED, because those are
        # different facts and conflating them is what the 3.5.1 verdict did.
        $detail += "; $($Result.exited) of $issued confirmed gone"
    }
    if ($Result.spared -gt 0) {
        $detail += "; left $($Result.spared) Qdrant process(es) alone - outside this " +
                   'installation, so not ours to stop'
    }
    return $detail
}

# ---------------------------------------------------------------------------
# The phases
# ---------------------------------------------------------------------------

function Get-InstalledVersion {
    $key = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E4B2A3C-F1D8-4A5E-B9C0-1234567890AB}_is1'
    foreach ($hive in @('HKCU:', 'HKLM:')) {
        try {
            $entry = Get-ItemProperty -Path "$hive\$key" -ErrorAction Stop
            if ($entry -and $entry.DisplayVersion) { return [string] $entry.DisplayVersion }
        }
        catch { }
    }
    return ''
}

function Invoke-DetectInstallation {
    $exe = Join-Path $AppDir 'rag.exe'
    $version = Get-InstalledVersion
    if (-not (Test-Path -LiteralPath $exe) -and -not $version) {
        return @{ outcome = 'skipped'
                  detail  = 'no previous installation; a fresh install pays for none of this' }
    }
    $Script:Installation = @{
        present    = $true
        version    = $version
        executable = $exe
    }
    return @{ detail = "v$(if ($version) { $version } else { 'unknown' }) at $AppDir" }
}

function Invoke-EnterMaintenance {
    # Best effort, over the service's own HTTP interface. Most of the versions
    # this runs against were published before an upgrade-maintenance state
    # existed, so its absence is expected and is not a failure.
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:21420/api/maintenance/upgrade' `
                                      -Method Post -TimeoutSec 5 -UseBasicParsing `
                                      -ErrorAction Stop
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
            return @{ detail = 'installed version entered upgrade maintenance' }
        }
    }
    catch { }
    return @{ outcome = 'skipped'
              detail  = 'installed version does not support an upgrade-maintenance state; absence is not a failure' }
}

function Get-TaskState {
    # A task that is not registered is 'Missing' - a fact, not a failure. It is
    # the EXPECTED answer for at least one of $Script:OwnedTasks on every
    # release this upgrades from, so it must be returnable. Through Invoke-Native
    # because `schtasks` announces it on stderr; see that function for what a
    # bare `2>$null` did here.
    param([string] $Name)
    $result = Invoke-Native -FilePath "$env:SystemRoot\System32\schtasks.exe" `
                            -ArgumentList @('/query', '/tn', $Name, '/fo', 'list')
    if ($result.Failed -or $result.ExitCode -ne 0) { return 'Missing' }
    foreach ($line in @($result.Lines)) {
        if ($line -match '^\s*Status:\s*(.+?)\s*$') { return $Matches[1] }
    }
    return 'Unknown'
}

function Invoke-DisableTasks {
    <#
        DISABLE, not /end.

        /end stops the running instance and leaves the trigger armed, so the
        scheduler is free to start a replacement between the kill and the copy -
        and the registration carries RestartOnFailure, which a force-kill is
        precisely the failure it reacts to. That is the restart race, and /end
        does not close it.

        The prior state is recorded because the upgrade has to put it back, and
        a task the user had already disabled must STAY disabled.
    #>
    foreach ($name in $Script:OwnedTasks) {
        $prior = Get-TaskState -Name $name
        if ($prior -eq 'Missing' -or -not $prior) { continue }
        if ($prior -ieq 'Disabled') {
            [void] $Script:TaskRecords.Add([ordered]@{
                name = $name; prior_state = $prior; disabled_by_us = $false })
            continue
        }
        $code = Invoke-Bounded -FilePath "$env:SystemRoot\System32\schtasks.exe" `
                               -ArgumentList @('/change', '/tn', "`"$name`"", '/disable') `
                               -TimeoutMilliseconds 30000
        [void] $Script:TaskRecords.Add([ordered]@{
            name = $name; prior_state = $prior; disabled_by_us = ($code -eq 0) })
    }
    if ($Script:TaskRecords.Count -eq 0) {
        return @{ outcome = 'skipped'; detail = 'no owned scheduled task is registered' }
    }
    $disabled = @($Script:TaskRecords | Where-Object { $_.disabled_by_us } |
                  ForEach-Object { $_.name })
    $detail = "disabled $($disabled.Count) of $($Script:TaskRecords.Count) owned task(s)"
    if ($disabled.Count -gt 0) { $detail += ": $([string]::Join(', ', $disabled))" }
    return @{ detail = $detail }
}

function Invoke-IdentifyHolders {
    <#
        Identify any remaining process whose executable OR any loaded module
        lies under -AppDir. Image-name matching is what the previous
        implementation did and it is insufficient: a process with any other name
        that has loaded _internal\python312.dll holds the lock just as hard.
    #>
    $owned = @(Get-OwnedProcess)
    $found = New-Object System.Collections.ArrayList
    foreach ($proc in $owned) { [void] $found.Add((Format-ProcessBlocker -Process $proc)) }
    # Same rule as Get-OwnedProcess: zero modules is a scan that did not happen,
    # not a process with nothing mapped. Counting an empty result as a successful
    # scan is what made this line report 0 on a host that could read none of them.
    $unscannable = @(Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
        $moduleCount = 0
        try { $moduleCount = @($_.Modules).Count } catch { $moduleCount = 0 }
        if ($moduleCount -eq 0) { $_.Id }
    })
    $detail = "$($owned.Count) owned process(es) still running"
    if ($unscannable.Count -gt 0) {
        $detail += "; $($unscannable.Count) process(es) refused a module scan - recorded, " +
                   'not dropped; the replaceability probe is what decides'
    }
    return @{ outcome = $(if ($owned.Count -gt 0) { 'blocked' } else { 'ok' })
              detail = $detail; blockers = @($found) }
}

function Invoke-WaitForExit {
    # A signal delivered is not a handle released. taskkill returns once the
    # signal is delivered, not once the kernel has released the image's file
    # handle, and the copy that follows fails on exactly that gap - so this
    # always sleeps at least once, even when nothing is left.
    $slept = 0
    for ($round = 0; $round -lt $Script:SettleRounds; $round++) {
        Start-Sleep -Milliseconds $Script:SettleMilliseconds
        $slept += $Script:SettleMilliseconds
        if ((@(Get-OwnedProcess).Count -eq 0) -or (Get-DeadlineExceeded)) { break }
    }
    $remaining = @(Get-OwnedProcess)
    if (($remaining.Count -gt 0) -and (Get-DeadlineExceeded)) {
        $Script:TimedOut = $true
        return @{ outcome = 'blocked'
                  detail  = "timeout after $TimeoutSeconds s with $($remaining.Count) owned " +
                            'process(es) still running - refusing BEFORE the first write, ' +
                            'so the installation is untouched' }
    }
    if ($remaining.Count -gt 0) {
        return @{ outcome = 'blocked'
                  detail  = "$($remaining.Count) owned process(es) still running after $slept ms" }
    }
    return @{ detail = "settled after $slept ms" }
}

function Invoke-RetrySafeShutdown {
    # Only the safe half, and only while there is time. Nothing here re-runs
    # disable_tasks, the one earlier phase whose second run would destroy
    # information rather than merely repeat work.
    if (@(Get-OwnedProcess).Count -eq 0) {
        return @{ outcome = 'skipped'; detail = 'nothing left to retry' }
    }
    $attempts = 0
    foreach ($backoff in $Script:RetryBackoffSeconds) {
        if (Get-DeadlineExceeded) { $Script:TimedOut = $true; break }
        $attempts++
        Start-Sleep -Seconds $backoff
        foreach ($phase in $Script:RetryablePhases) {
            $role = switch ($phase) {
                'stop_tray'             { 'tray' }
                'stop_supervisor'       { 'supervisor' }
                'stop_service_children' { 'service' }
                'stop_managed_engine'   { 'engine' }
            }
            [void] (Invoke-RoleStop -Role $role)
        }
        if (@(Get-OwnedProcess).Count -eq 0) { break }
    }
    $remaining = @(Get-OwnedProcess)
    if ($remaining.Count -gt 0) {
        return @{ outcome = 'blocked'
                  detail  = "$attempts retry attempt(s); $($remaining.Count) process(es) still holding installation files" }
    }
    return @{ detail = "cleared after $attempts retry attempt(s)" }
}

function Invoke-VerifyClear {
    $owned = @(Get-OwnedProcess)
    $found = New-Object System.Collections.ArrayList
    foreach ($proc in $owned) { [void] $found.Add((Format-ProcessBlocker -Process $proc)) }
    if ($owned.Count -gt 0) {
        return @{ outcome = 'blocked'
                  detail  = "$($owned.Count) owned process(es) survived every stop"
                  blockers = @($found) }
    }
    $detail = 'no owned process is running'
    # "Nothing is running" and "nothing could be identified" are opposite facts
    # that used to read identically. When the process table cannot be answered,
    # path scoping is BLIND - and the engine's ownership is nothing but path.
    if ((Get-ProcessTable).Count -eq 0) {
        $detail += '; the process table could not be read, so this is the absence ' +
                   'of evidence and not evidence of absence - the replaceability ' +
                   'probe is what decides'
    }
    return @{ detail = $detail }
}

function Test-Replaceable {
    <#
        Proof, not inference. The file is opened with FileShare.None - exactly
        what Windows requires of the delete and the overwrite that follow - and
        closed again immediately. Nothing is read from it and nothing is written
        to it.

        This is the step that would have caught the real failure: process
        enumeration can come back clean while a handle survives (a duplicated
        handle, a driver, an indexer, an antivirus scan), and the first thing
        that discovers it today is [InstallDelete], mid-write.
    #>
    param([string] $Path)
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
                                         [System.IO.FileAccess]::ReadWrite,
                                         [System.IO.FileShare]::None)
        return @{ ok = $true; reason = '' }
    }
    catch [System.IO.IOException] {
        return @{ ok = $false
                  reason = "in use by another process - $($_.Exception.Message)" }
    }
    catch [System.UnauthorizedAccessException] {
        return @{ ok = $false
                  reason = "cannot be opened for replacement - $($_.Exception.Message)" }
    }
    catch {
        return @{ ok = $false; reason = $_.Exception.Message }
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Invoke-ProbeReplaceable {
    if (-not (Test-Path -LiteralPath $AppDir)) {
        return @{ outcome = 'skipped'; detail = 'no installation directory to probe' }
    }
    $found = New-Object System.Collections.ArrayList
    $checked = 0
    foreach ($file in @(Get-ChildItem -LiteralPath $AppDir -Recurse -File -ErrorAction SilentlyContinue)) {
        $suffix = $file.Extension.ToLowerInvariant()
        if ($Script:ReplaceableSuffixes -notcontains $suffix) { continue }
        $checked++
        $verdict = Test-Replaceable -Path $file.FullName
        if (-not $verdict.ok) {
            [void] $found.Add((Add-Blocker -Kind 'file' -Identity $file.FullName `
                                           -Detail $verdict.reason -Path $file.FullName))
        }
    }
    if ($found.Count -gt 0) {
        return @{ outcome = 'blocked'
                  detail  = "$($found.Count) of $checked file(s) cannot be replaced"
                  blockers = @($found) }
    }
    return @{ detail = "$checked file(s) proven replaceable" }
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

function Get-LogDirectory {
    $dir = Split-Path -Parent $LogPath
    if (-not $dir) { $dir = '.' }
    if (-not (Test-Path -LiteralPath $dir)) {
        [void] (New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue)
    }
    return $dir
}

function Write-TextFile {
    <#
        UTF-8 WITHOUT a BOM, deliberately.

        Windows PowerShell's `Set-Content -Encoding UTF8` writes one, and both
        consumers of these files reject it: Inno's LoadStringFromFile reads
        bytes, so the BOM would arrive as three stray characters at the top of
        the refusal dialog, and Python's json.load raises on it unless the
        caller happens to have chosen utf-8-sig.
    #>
    param([string] $Path, [string] $Text)
    try {
        [System.IO.File]::WriteAllText($Path, $Text,
                                       (New-Object System.Text.UTF8Encoding($false)))
    }
    catch { }
}

function Write-Verdict {
    param([int] $ExitCode, [bool] $Quiescent, [string] $Reason)

    # The verdict document. Key for key, this is
    # ragtools.upgrade.quiescence.Verdict.to_json().
    $verdict = [ordered]@{
        schema           = $Script:Schema
        mode             = 'quiesce'
        exit_code        = $ExitCode
        quiescent        = $Quiescent
        reason           = $Reason
        app_dir          = $AppDir
        data_dir         = $DataDir
        installation     = $Script:Installation
        elapsed_seconds  = [math]::Round($Script:Started.Elapsed.TotalSeconds, 3)
        phases           = @($Script:PhaseRecords)
        blockers         = @($Script:Blockers)
        tasks            = @($Script:TaskRecords)
    }

    $dir = Get-LogDirectory
    Write-TextFile -Path $LogPath -Text ($verdict | ConvertTo-Json -Depth 8)

    # A second, plain-text file, because Inno's Pascal reads a string far more
    # reliably than it parses JSON, and the message the user sees has to name
    # what to close - not tell them to reboot.
    $lines = New-Object System.Collections.ArrayList
    if ($Quiescent) {
        [void] $lines.Add('RAG Tools can be upgraded: nothing is holding its files.')
    }
    else {
        [void] $lines.Add('RAG Tools cannot be upgraded safely right now, so nothing has been changed and your current installation is still intact.')
        [void] $lines.Add('')
        [void] $lines.Add('Still holding the files that must be replaced:')
        foreach ($b in @($Script:Blockers)) {
            [void] $lines.Add("  - $($b.identity): $($b.detail)")
        }
        if (@($Script:Blockers).Count -eq 0) {
            [void] $lines.Add("  - (no individual blocker recorded; reason: $Reason)")
        }
    }
    Write-TextFile -Path (Join-Path $dir 'quiesce-blockers.txt') `
                   -Text ([string]::Join([Environment]::NewLine, @($lines)))

    # The task record, in the simplest form a restore pass can consume: one
    # name per line, only the ones this run actually disabled.
    $restore = @($Script:TaskRecords | Where-Object { $_.disabled_by_us } |
                 ForEach-Object { $_.name })
    Write-TextFile -Path (Join-Path $dir 'quiesce-restore.txt') `
                   -Text ([string]::Join([Environment]::NewLine, @($restore)))
}

# ---------------------------------------------------------------------------
# Restore - re-enable exactly what Quiesce disabled, and nothing else
# ---------------------------------------------------------------------------

function Invoke-RestoreTaskState {
    $dir = Get-LogDirectory
    $record = Join-Path $dir 'quiesce-restore.txt'
    if (-not (Test-Path -LiteralPath $record)) {
        Write-Host "$($Script:RestorePhase): nothing was disabled, nothing to restore"
        return
    }
    $restored = 0
    foreach ($name in @(Get-Content -LiteralPath $record -ErrorAction SilentlyContinue)) {
        $trimmed = $name.Trim()
        if (-not $trimmed) { continue }
        # A task the user had already disabled is NOT in this file, so it stays
        # disabled. Re-enabling everything "to be safe" would silently switch
        # autostart back on for someone who turned it off.
        $code = Invoke-Bounded -FilePath "$env:SystemRoot\System32\schtasks.exe" `
                               -ArgumentList @('/change', '/tn', "`"$trimmed`"", '/enable') `
                               -TimeoutMilliseconds 30000
        if ($code -eq 0) { $restored++ }
    }
    Write-Host "$($Script:RestorePhase): re-enabled $restored task(s)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if ($Mode -eq 'Restore') {
    try {
        Invoke-RestoreTaskState
        exit $Script:ExitCodes['quiescent']
    }
    catch { exit $Script:ExitCodes['internal_error'] }
}

try {
    Invoke-Phase -Name 'detect_installation' -Body { Invoke-DetectInstallation }

    if (-not $Script:Installation['present']) {
        foreach ($phase in $Script:Phases) {
            if ($phase -eq 'detect_installation') { continue }
            Add-SkippedPhase -Name $phase -Detail 'no_previous_installation'
        }
        Write-Verdict -ExitCode $Script:ExitCodes['quiescent'] -Quiescent $true `
                      -Reason 'no_previous_installation'
        exit $Script:ExitCodes['quiescent']
    }

    Invoke-Phase -Name 'enter_maintenance'     -Body { Invoke-EnterMaintenance }
    Invoke-Phase -Name 'disable_tasks'         -Body { Invoke-DisableTasks }
    Invoke-Phase -Name 'stop_tray'             -Body { @{ detail = (Format-StopDetail -Result (Invoke-RoleStop -Role 'tray')       -Role 'tray') } }
    Invoke-Phase -Name 'stop_supervisor'       -Body { @{ detail = (Format-StopDetail -Result (Invoke-RoleStop -Role 'supervisor') -Role 'supervisor') } }
    Invoke-Phase -Name 'stop_service_children' -Body { @{ detail = (Format-StopDetail -Result (Invoke-RoleStop -Role 'service')    -Role 'service') } }
    Invoke-Phase -Name 'stop_managed_engine'   -Body { @{ detail = (Format-StopDetail -Result (Invoke-RoleStop -Role 'engine')     -Role 'engine') } }
    Invoke-Phase -Name 'identify_holders'      -Body { Invoke-IdentifyHolders }
    Invoke-Phase -Name 'wait_for_exit'         -Body { Invoke-WaitForExit }
    Invoke-Phase -Name 'retry_safe_shutdown'   -Body { Invoke-RetrySafeShutdown }

    # identify_holders' findings were a snapshot mid-protocol. Only the last two
    # phases decide, so the blocker list is rebuilt from them.
    $Script:Blockers.Clear()

    Invoke-Phase -Name 'verify_clear'          -Body { Invoke-VerifyClear }
    # Run regardless of what the process sweep found: fixing one blocker at a
    # time across five failed attempts is a miserable way to upgrade.
    Invoke-Phase -Name 'probe_replaceable'     -Body { Invoke-ProbeReplaceable }

    $processBlockers = @($Script:Blockers | Where-Object { $_.kind -eq 'process' })

    if (@($Script:Blockers).Count -eq 0) {
        Write-Verdict -ExitCode $Script:ExitCodes['quiescent'] -Quiescent $true -Reason 'quiescent'
        exit $Script:ExitCodes['quiescent']
    }

    $reason = 'files_not_replaceable'
    if ($Script:TimedOut)              { $reason = 'timeout' }
    elseif ($processBlockers.Count -gt 0) { $reason = 'processes_survived' }

    Write-Verdict -ExitCode $Script:ExitCodes['not_quiescent'] -Quiescent $false -Reason $reason
    exit $Script:ExitCodes['not_quiescent']
}
catch {
    try {
        [void] (Add-Blocker -Kind 'internal' -Identity $_.Exception.GetType().Name `
                            -Detail $_.Exception.Message)
        Write-Verdict -ExitCode $Script:ExitCodes['internal_error'] -Quiescent $false `
                      -Reason 'internal_error'
    }
    catch { }
    exit $Script:ExitCodes['internal_error']
}
