#Requires -Version 5
<#
.SYNOPSIS
데스게임 보고서 한국어 패치 설치기 (Windows 기본 PowerShell 전용, 외부 의존성 없음).

.DESCRIPTION
tools/patch_release.py 의 install/uninstall 경로를 그대로 이식한 단독 설치기다.
정확한 원본 빌드 해시를 확인한 뒤에만 동작하고, 원본 PCK 를 해시 검증된 백업으로
보관한 다음 base-dependent 델타를 적용해 원자적으로 교체한다.

패키지 제작 시점의 원본 미포함 감사(`_assert_no_base_block_in_adds`, 원본 전체
임베딩 스캔)는 배포자가 `python3 tools/patch_release.py audit` 으로 수행하는
producer-side 검사이므로 이 설치기에는 포함하지 않는다. 설치 시점에 필요한
호환성·무결성 검사는 모두 동일하게 수행한다.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('audit', 'install', 'uninstall')]
    [string]$Command,

    [string]$PackageDir,
    [string]$GameDir,

    # 테스트 전용 핀 주입. 생략하면 배포 대상 빌드의 고정 해시를 사용한다.
    [string]$ExpectedExeSha256,
    [string]$ExpectedPckSha256
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:TargetBuildId = 'DGR-WIN-1.0.5-b3e7048e'
$script:TargetExeSha256 = '1de3edb8e10c66412a5e0d76ca44a2b70ad791d8b08f17135aae36b9bc369bf5'
$script:TargetPckSha256 = 'b3e7048e62421e74b8b485f242fbec3f14cde94d452309f6d57d957623821399'
$script:ReleaseSchemaVersion = 2
$script:DeltaVersion = 1
$script:DeltaMagic = 'DREPO-KO-DELTA01'
$script:MinBlockSize = 4096
$script:MinCopyRatio = 0.10
$script:MaxDeltaOutputSize = 268435456
$script:MaxDeltaOperations = 10000
$script:MaxManifestSize = 1048576
$script:MaxDeltaFileSize = 67108864
$script:DeltaFormat = 'drepo-copy-add-zlib-v1'

$script:ManifestName = 'manifest.json'
$script:DeltaName = 'drepo.pck.delta'
$script:InstallerPs1Name = 'patch_release.ps1'
$script:InstallCmdName = 'install.cmd'
$script:UninstallCmdName = 'uninstall.cmd'
$script:InstallDocName = 'INSTALL_KO.md'
$script:FontLicenseName = 'FONT_LICENSE_OFL-1.1.txt'

$script:BackupDirName = '.drepo-ko-backup'
$script:BackupPckName = 'drepo.pck.original'
$script:StateName = 'install-state.json'
$script:GameExeName = 'drepo.exe'
$script:GamePckName = 'drepo.pck'
$script:InstallTempName = '.drepo.pck.ko-install.tmp'
$script:RestoreTempName = '.drepo.pck.ko-restore.tmp'
$script:SwapTempName = '.drepo.pck.ko-swap.tmp'

$script:ReplaceMode = 'none'
$script:HeaderSize = 40
$script:OpSize = 17
$script:CopyOpcode = 1
$script:AddOpcode = 2


function Fail {
    param([string]$Message)
    throw [System.InvalidOperationException]::new($Message)
}

function Get-Field {
    # StrictMode 아래에서 없는 속성 접근이 예외가 되지 않도록 감싼다.
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-FieldNames {
    param($Object)
    if ($null -eq $Object) { return @() }
    return @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Test-HexSha256 {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return $false }
    return ($Value -cmatch '^[0-9a-f]{64}$')
}

function Get-Sha256Bytes {
    param([byte[]]$Bytes)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes)) -replace '-', '').ToLowerInvariant()
    } finally {
        $sha.Dispose()
    }
}

function Get-Sha256File {
    param([string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $sha.Dispose()
    }
}

function Assert-RegularFile {
    param([string]$Path, [string]$Label)
    if (-not [System.IO.File]::Exists($Path)) {
        Fail "$Label 이(가) 일반 파일이 아닙니다: $Path"
    }
    $attributes = [System.IO.File]::GetAttributes($Path)
    if ($attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Fail "$Label 이(가) 링크입니다. 실제 파일이어야 합니다: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).ProviderPath
}

function Assert-RealDirectory {
    param([string]$Path, [string]$Label)
    if (-not [System.IO.Directory]::Exists($Path)) {
        Fail "$Label 이(가) 실제 디렉터리가 아닙니다: $Path"
    }
    $attributes = [System.IO.File]::GetAttributes($Path)
    if ($attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Fail "$Label 이(가) 링크입니다. 실제 디렉터리여야 합니다: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).ProviderPath
}

function Assert-FileNotLocked {
    <#
    게임을 켜 둔 채 실행하는 것이 가장 흔한 실수다. 어떤 파일도 건드리기 전에
    쓰기 권한과 배타적 접근을 확인해 원인이 분명한 한국어 안내로 중단한다.
    #>
    param([string]$Path, [string]$Label)

    try {
        $handle = [System.IO.File]::Open(
            $Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        $handle.Dispose()
    } catch [System.IO.IOException] {
        Fail "$Label 을(를) 다른 프로그램이 사용 중입니다. 게임을 완전히 종료한 뒤 다시 실행해 주세요: $Path"
    } catch [System.UnauthorizedAccessException] {
        Fail "$Label 에 쓸 권한이 없습니다. 게임 폴더 권한을 확인해 주세요: $Path"
    }
}

function Assert-FileHash {
    param([string]$Path, [string]$Expected, [string]$Label)
    if (-not (Test-HexSha256 $Expected)) {
        Fail "$Label 의 SHA-256 핀 형식이 올바르지 않습니다"
    }
    $actual = Get-Sha256File -Path $Path
    if ($actual -cne $Expected) {
        Fail "$Label SHA-256 불일치: 기대값 $Expected, 실제값 $actual"
    }
    return $actual
}

function Read-JsonObject {
    param([string]$Path, [string]$Label)
    $size = (New-Object System.IO.FileInfo($Path)).Length
    if ($size -gt $script:MaxManifestSize) {
        Fail "$Label 크기가 제한을 넘습니다: $size 바이트"
    }
    try {
        $text = [System.IO.File]::ReadAllText($Path, [System.Text.UTF8Encoding]::new($false, $true))
    } catch {
        Fail "$Label 을(를) UTF-8로 읽을 수 없습니다: $($_.Exception.Message)"
    }
    try {
        $value = ConvertFrom-Json -InputObject $text
    } catch {
        Fail "$Label 이(가) 올바른 JSON 이 아닙니다: $($_.Exception.Message)"
    }
    if ($null -eq $value -or -not ($value -is [System.Management.Automation.PSCustomObject])) {
        Fail "$Label 은(는) JSON 객체여야 합니다"
    }
    return $value
}


function ConvertTo-Int64Checked {
    param([UInt64]$Value, [string]$Label)
    if ($Value -gt [UInt64][Int64]::MaxValue) {
        Fail "$Label 값이 처리 가능한 범위를 넘습니다"
    }
    return [Int64]$Value
}

function Read-DeltaPlan {
    <#
    델타를 한 번 훑어 구조와 통계를 검증한다. tools/patch_release.py 의
    decode_delta 와 동일한 가드를 적용하되 페이로드 해제는 뒤로 미룬다.
    #>
    param([byte[]]$Delta, [Int64]$BaseSize)

    $length = [Int64]$Delta.Length
    if ($length -lt $script:HeaderSize) {
        Fail '델타가 헤더 전에 잘렸습니다'
    }
    $magic = [System.Text.Encoding]::ASCII.GetString($Delta, 0, 16)
    $version = [System.BitConverter]::ToUInt32($Delta, 16)
    $blockSize = [System.BitConverter]::ToUInt32($Delta, 20)
    $targetSize = ConvertTo-Int64Checked ([System.BitConverter]::ToUInt64($Delta, 24)) '델타 target_size'
    $operationCount = ConvertTo-Int64Checked ([System.BitConverter]::ToUInt64($Delta, 32)) '델타 operation_count'

    if ($magic -cne $script:DeltaMagic -or $version -ne $script:DeltaVersion) {
        Fail '델타 매직/버전이 일치하지 않습니다'
    }
    if ($blockSize -lt $script:MinBlockSize) {
        Fail '델타 블록 크기가 안전하지 않습니다'
    }
    if ($targetSize -le 0 -or $targetSize -gt $script:MaxDeltaOutputSize -or $operationCount -le 0) {
        Fail '델타가 빈 출력 또는 빈 오퍼레이션 목록을 선언했습니다'
    }
    if ($operationCount -gt $script:MaxDeltaOperations -or $operationCount -gt ($targetSize + 1)) {
        Fail '델타 오퍼레이션 수가 비정상입니다'
    }

    $operations = New-Object System.Collections.ArrayList
    $cursor = [Int64]$script:HeaderSize
    $emitted = [Int64]0
    $copyBytes = [Int64]0
    $addBytes = [Int64]0

    for ($index = 0; $index -lt $operationCount; $index++) {
        if ($cursor -ge $length) {
            Fail '델타 오퍼레이션 목록이 잘렸습니다'
        }
        $opcode = $Delta[$cursor]
        if ($cursor + $script:OpSize -gt $length) {
            Fail '델타 오퍼레이션 헤더가 잘렸습니다'
        }
        $first = ConvertTo-Int64Checked ([System.BitConverter]::ToUInt64($Delta, $cursor + 1)) '델타 오퍼레이션 필드'
        $second = ConvertTo-Int64Checked ([System.BitConverter]::ToUInt64($Delta, $cursor + 9)) '델타 오퍼레이션 필드'
        $cursor += $script:OpSize

        if ($opcode -eq $script:CopyOpcode) {
            if ($second -le 0 -or $first -gt $BaseSize -or $second -gt ($BaseSize - $first)) {
                Fail '델타 COPY 오퍼레이션이 원본 PCK 범위를 벗어납니다'
            }
            [void]$operations.Add([pscustomobject]@{
                Kind = 'copy'
                Offset = $first
                Length = $second
            })
            $copyBytes += $second
            $emitted += $second
        } elseif ($opcode -eq $script:AddOpcode) {
            if ($first -le 0 -or $second -le 0 -or ($emitted + $first) -gt $targetSize) {
                Fail '델타 ADD 오퍼레이션 길이가 안전하지 않습니다'
            }
            if ($cursor + $second -gt $length) {
                Fail '델타 ADD 페이로드가 잘렸습니다'
            }
            [void]$operations.Add([pscustomobject]@{
                Kind = 'add'
                RawLength = $first
                StoredLength = $second
                PayloadOffset = $cursor
            })
            $cursor += $second
            $addBytes += $first
            $emitted += $first
        } else {
            Fail "델타에 알 수 없는 opcode 가 있습니다: $opcode"
        }
        if ($emitted -gt $targetSize) {
            Fail '델타가 선언보다 많은 바이트를 생성합니다'
        }
    }

    if ($cursor -ne $length) {
        Fail '델타 뒤에 잔여 바이트가 있습니다'
    }
    if ($emitted -ne $targetSize) {
        Fail "델타 출력 크기 불일치: 선언 $targetSize, 오퍼레이션 합계 $emitted"
    }

    return [pscustomobject]@{
        Operations = $operations
        Metrics = [pscustomobject]@{
            block_size = [Int64]$blockSize
            target_size = $targetSize
            operation_count = $operationCount
            copy_bytes = $copyBytes
            add_bytes = $addBytes
        }
    }
}

function Expand-ZlibInto {
    <#
    zlib(RFC1950) 블록을 정확히 RawLength 바이트만 출력 버퍼에 해제한다.
    .NET Framework 에는 zlib 래퍼가 없으므로 2바이트 헤더를 직접 검증하고
    deflate 본문만 DeflateStream 에 넘긴다. adler32 트레일러는 검사하지 않으며
    최종 산출물 전체의 SHA-256 검증이 그 역할을 대신한다.
    #>
    param(
        [System.IO.MemoryStream]$Source,
        [byte[]]$Delta,
        [Int64]$PayloadOffset,
        [Int64]$StoredLength,
        [Int64]$RawLength,
        [byte[]]$Output,
        [Int64]$OutputOffset
    )

    if ($StoredLength -lt 6) {
        Fail '델타 ADD 페이로드가 zlib 스트림으로 보기에 너무 짧습니다'
    }
    $cmf = $Delta[$PayloadOffset]
    $flg = $Delta[$PayloadOffset + 1]
    if (($cmf -band 0x0f) -ne 8) {
        Fail '델타 ADD 페이로드의 zlib 압축 방식이 deflate 가 아닙니다'
    }
    if (((([int]$cmf) * 256) + [int]$flg) % 31 -ne 0) {
        Fail '델타 ADD 페이로드의 zlib 헤더 검사값이 올바르지 않습니다'
    }
    if ($flg -band 0x20) {
        Fail '델타 ADD 페이로드가 preset dictionary를 사용합니다'
    }

    $Source.Position = $PayloadOffset + 2
    $inflater = New-Object System.IO.Compression.DeflateStream($Source, [System.IO.Compression.CompressionMode]::Decompress, $true)
    try {
        $done = [Int64]0
        while ($done -lt $RawLength) {
            $chunk = $RawLength - $done
            if ($chunk -gt 1048576) { $chunk = 1048576 }
            $read = $inflater.Read($Output, $OutputOffset + $done, [int]$chunk)
            if ($read -le 0) {
                Fail '델타 ADD 페이로드가 선언된 크기보다 작습니다'
            }
            $done += $read
        }
        $tail = New-Object byte[] 1
        if ($inflater.Read($tail, 0, 1) -ne 0) {
            Fail '델타 ADD 페이로드가 선언된 크기를 넘어 확장됩니다'
        }
    } catch [System.IO.InvalidDataException] {
        Fail "델타 ADD 페이로드가 손상됐습니다: $($_.Exception.Message)"
    } finally {
        $inflater.Dispose()
    }
}

function Invoke-DeltaApply {
    param([byte[]]$Base, [byte[]]$Delta, $Plan)

    $output = New-Object byte[] ([int]$Plan.Metrics.target_size)
    $position = [Int64]0
    $source = New-Object System.IO.MemoryStream(, $Delta)
    try {
        foreach ($operation in $Plan.Operations) {
            if ($operation.Kind -eq 'copy') {
                [System.Array]::Copy($Base, $operation.Offset, $output, $position, $operation.Length)
                $position += $operation.Length
            } else {
                Expand-ZlibInto -Source $source -Delta $Delta -PayloadOffset $operation.PayloadOffset `
                    -StoredLength $operation.StoredLength -RawLength $operation.RawLength `
                    -Output $output -OutputOffset $position
                $position += $operation.RawLength
            }
        }
    } finally {
        $source.Dispose()
    }
    if ($position -ne $Plan.Metrics.target_size) {
        Fail '델타 적용 결과 크기가 선언과 다릅니다'
    }
    return $output
}


function Assert-ManifestContract {
    param($Manifest, [string]$ExpectedExeSha256, [string]$ExpectedPckSha256)

    if ((Get-Field $Manifest 'schema_version') -ne $script:ReleaseSchemaVersion) {
        Fail '릴리스 매니페스트 스키마 버전이 일치하지 않습니다'
    }
    if ((Get-Field $Manifest 'build_id') -cne $script:TargetBuildId -or (Get-Field $Manifest 'language') -cne 'ko-KR') {
        Fail '릴리스 매니페스트의 대상 빌드 식별값이 일치하지 않습니다'
    }
    $gameFiles = Get-Field $Manifest 'game_files'
    if ((Get-Field $gameFiles 'executable') -cne $script:GameExeName -or (Get-Field $gameFiles 'pck') -cne $script:GamePckName) {
        Fail '릴리스 매니페스트의 게임 파일 경로가 고정 안전 이름이 아닙니다'
    }
    $source = Get-Field $Manifest 'source'
    if ((Get-Field $source 'executable_sha256') -cne $ExpectedExeSha256) {
        Fail '릴리스 매니페스트의 실행 파일 핀이 일치하지 않습니다'
    }
    if ((Get-Field $source 'pck_sha256') -cne $ExpectedPckSha256) {
        Fail '릴리스 매니페스트의 PCK 핀이 일치하지 않습니다'
    }
    $delta = Get-Field $Manifest 'delta'
    if ((Get-Field $delta 'file') -cne $script:DeltaName) {
        Fail '릴리스 매니페스트의 델타 경로가 일치하지 않습니다'
    }
    if ((Get-Field $delta 'format') -cne $script:DeltaFormat) {
        Fail '릴리스 매니페스트의 델타 포맷이 일치하지 않습니다'
    }
    $installerFiles = Get-Field (Get-Field $Manifest 'installer') 'files'
    $installerNames = @(Get-FieldNames $installerFiles | Sort-Object)
    $expectedInstallerNames = @($script:InstallCmdName, $script:InstallerPs1Name, $script:UninstallCmdName) | Sort-Object
    if (Compare-Object -ReferenceObject $expectedInstallerNames -DifferenceObject $installerNames) {
        Fail '릴리스 매니페스트의 설치기 파일 목록이 일치하지 않습니다'
    }
    if ((Get-Field (Get-Field $Manifest 'documentation') 'file') -cne $script:InstallDocName) {
        Fail '릴리스 매니페스트의 설치 문서 경로가 일치하지 않습니다'
    }
    if ((Get-Field (Get-Field $Manifest 'font_license') 'file') -cne $script:FontLicenseName) {
        Fail '릴리스 매니페스트의 폰트 라이선스 경로가 일치하지 않습니다'
    }
    $paths = Get-Field $Manifest 'install_paths'
    if (
        (Get-Field $paths 'backup_directory') -cne $script:BackupDirName -or
        (Get-Field $paths 'backup_pck') -cne "$($script:BackupDirName)/$($script:BackupPckName)" -or
        (Get-Field $paths 'state') -cne "$($script:BackupDirName)/$($script:StateName)" -or
        (Get-Field $paths 'install_temporary') -cne $script:InstallTempName -or
        (Get-Field $paths 'restore_temporary') -cne $script:RestoreTempName -or
        (Get-Field $paths 'swap_temporary') -cne $script:SwapTempName
    ) {
        Fail '릴리스 매니페스트의 설치 경로가 고정 안전 경로가 아닙니다'
    }
}

function Invoke-PackageAudit {
    param(
        [string]$PackageDir,
        [string]$ExePath,
        [string]$PckPath,
        [string]$ExpectedExeSha256,
        [string]$ExpectedPckSha256
    )

    $package = Assert-RealDirectory -Path $PackageDir -Label '패치 폴더'
    $exe = Assert-RegularFile -Path $ExePath -Label '게임 실행 파일'
    $pck = Assert-RegularFile -Path $PckPath -Label '게임 PCK'
    [void](Assert-FileHash -Path $exe -Expected $ExpectedExeSha256 -Label '게임 실행 파일')
    [void](Assert-FileHash -Path $pck -Expected $ExpectedPckSha256 -Label '게임 PCK')

    $expectedNames = @(
        $script:ManifestName, $script:DeltaName, $script:InstallerPs1Name,
        $script:InstallCmdName, $script:UninstallCmdName,
        $script:InstallDocName, $script:FontLicenseName
    ) | Sort-Object
    $actualNames = New-Object System.Collections.ArrayList
    foreach ($entry in [System.IO.Directory]::GetFileSystemEntries($package)) {
        $name = [System.IO.Path]::GetFileName($entry)
        if (-not [System.IO.File]::Exists($entry)) {
            Fail "패치 폴더에 일반 파일이 아닌 항목이 있습니다: $name"
        }
        if ([System.IO.File]::GetAttributes($entry) -band [System.IO.FileAttributes]::ReparsePoint) {
            Fail "패치 폴더에 링크 항목이 있습니다: $name"
        }
        [void]$actualNames.Add($name)
    }
    $sortedActual = @($actualNames | Sort-Object)
    if (Compare-Object -ReferenceObject $expectedNames -DifferenceObject $sortedActual) {
        Fail "패치 폴더 구성이 다릅니다. 기대: $($expectedNames -join ', ') / 실제: $($sortedActual -join ', ')"
    }

    $manifestPath = Join-Path $package $script:ManifestName
    $deltaPath = Join-Path $package $script:DeltaName
    $manifest = Read-JsonObject -Path $manifestPath -Label '릴리스 매니페스트'
    Assert-ManifestContract -Manifest $manifest -ExpectedExeSha256 $ExpectedExeSha256 -ExpectedPckSha256 $ExpectedPckSha256

    $deltaInfo = Get-Field $manifest 'delta'
    $deltaFileSize = (New-Object System.IO.FileInfo($deltaPath)).Length
    if ($deltaFileSize -gt $script:MaxDeltaFileSize) {
        Fail "델타 페이로드 크기가 제한을 넘습니다: $deltaFileSize 바이트"
    }
    [void](Assert-FileHash -Path $deltaPath -Expected ([string](Get-Field $deltaInfo 'sha256')) -Label '델타 페이로드')
    $installerFiles = Get-Field (Get-Field $manifest 'installer') 'files'
    foreach ($name in @($script:InstallerPs1Name, $script:InstallCmdName, $script:UninstallCmdName)) {
        [void](Assert-FileHash -Path (Join-Path $package $name) -Expected ([string](Get-Field $installerFiles $name)) -Label "설치기 파일 $name")
    }
    [void](Assert-FileHash -Path (Join-Path $package $script:InstallDocName) `
        -Expected ([string](Get-Field (Get-Field $manifest 'documentation') 'sha256')) -Label '설치 안내 문서')
    [void](Assert-FileHash -Path (Join-Path $package $script:FontLicenseName) `
        -Expected ([string](Get-Field (Get-Field $manifest 'font_license') 'sha256')) -Label 'Galmuri 폰트 라이선스')

    if ($deltaFileSize -ne (Get-Field $deltaInfo 'size')) {
        Fail '델타 파일 크기가 릴리스 매니페스트와 다릅니다'
    }
    $base = [System.IO.File]::ReadAllBytes($pck)
    if ((Get-Field (Get-Field $manifest 'source') 'pck_size') -ne $base.Length) {
        Fail '원본 PCK 크기가 릴리스 매니페스트와 다릅니다'
    }

    $deltaBytes = [System.IO.File]::ReadAllBytes($deltaPath)
    $plan = Read-DeltaPlan -Delta $deltaBytes -BaseSize ([Int64]$base.Length)
    foreach ($field in @('block_size', 'target_size', 'operation_count', 'copy_bytes', 'add_bytes')) {
        if ((Get-Field $deltaInfo $field) -ne (Get-Field $plan.Metrics $field)) {
            Fail "델타 통계 $field 이(가) 매니페스트와 다릅니다"
        }
    }

    $localizedInfo = Get-Field $manifest 'localized'
    $ratio = [double]$plan.Metrics.copy_bytes / [double]$plan.Metrics.target_size
    $dependency = Get-Field $manifest 'base_dependency'
    $declaredRatio = Get-Field $dependency 'copy_ratio'
    if (
        $ratio -lt $script:MinCopyRatio -or $null -eq $declaredRatio -or
        [math]::Abs([double]$declaredRatio - $ratio) -gt 1e-12 -or
        (Get-Field $dependency 'standalone_output_possible') -ne $false
    ) {
        Fail '릴리스 페이로드가 원본 의존성을 충분히 증명하지 못합니다'
    }

    $localized = Invoke-DeltaApply -Base $base -Delta $deltaBytes -Plan $plan
    if ($localized.Length -ne (Get-Field $localizedInfo 'pck_size')) {
        Fail '생성된 한국어 PCK 크기가 매니페스트와 다릅니다'
    }
    $localizedSha = Get-Sha256Bytes -Bytes $localized
    if ($localizedSha -cne [string](Get-Field $localizedInfo 'pck_sha256')) {
        Fail '생성된 한국어 PCK 해시가 매니페스트와 다릅니다'
    }

    return [pscustomobject]@{
        PackageDir = $package
        Manifest = $manifest
        ManifestSha256 = (Get-Sha256File -Path $manifestPath)
        LocalizedBytes = $localized
        LocalizedSha256 = $localizedSha
        DeltaMetrics = $plan.Metrics
    }
}


function Write-CanonicalStateFile {
    <#
    tools/patch_release.py 의 _json_bytes 와 바이트 단위로 동일한 상태 파일을
    만든다(키 정렬, 두 칸 들여쓰기, LF, 끝 개행, UTF-8, BOM 없음).
    ConvertTo-Json 은 포맷이 달라 사용하지 않는다.
    #>
    param([string]$Path, [string]$ManifestSha256, [string]$SourcePckSha256, [string]$LocalizedPckSha256)

    $lines = @(
        '{',
        "  `"backup_pck`": `"$($script:BackupPckName)`",",
        "  `"build_id`": `"$($script:TargetBuildId)`",",
        "  `"game_pck`": `"$($script:GamePckName)`",",
        "  `"localized_pck_sha256`": `"$LocalizedPckSha256`",",
        "  `"manifest_sha256`": `"$ManifestSha256`",",
        '  "schema_version": 1,',
        "  `"source_pck_sha256`": `"$SourcePckSha256`"",
        '}'
    )
    $text = ($lines -join "`n") + "`n"
    $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($text)
    Write-NewFile -Path $Path -Bytes $bytes
}

function Write-NewFile {
    param([string]$Path, [byte[]]$Bytes)
    if ([System.IO.File]::Exists($Path) -or [System.IO.Directory]::Exists($Path)) {
        Fail "기존 경로를 덮어쓰지 않습니다: $Path"
    }
    $stream = New-Object System.IO.FileStream($Path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    } finally {
        $stream.Dispose()
    }
}

function Assert-InstallState {
    param($State, [string]$ManifestSha256, [string]$SourcePckSha256, [string]$LocalizedPckSha256)
    $expected = [ordered]@{
        schema_version = 1
        build_id = $script:TargetBuildId
        manifest_sha256 = $ManifestSha256
        source_pck_sha256 = $SourcePckSha256
        localized_pck_sha256 = $LocalizedPckSha256
        game_pck = $script:GamePckName
        backup_pck = $script:BackupPckName
    }
    $stateNames = @(Get-FieldNames $State | Sort-Object)
    if (Compare-Object -ReferenceObject (@($expected.Keys) | Sort-Object) -DifferenceObject $stateNames) {
        Fail '설치 상태 파일이 올바르지 않거나 다른 패키지의 것입니다'
    }
    foreach ($key in $expected.Keys) {
        if ((Get-Field $State $key) -cne $expected[$key]) {
            Fail '설치 상태 파일이 올바르지 않거나 다른 패키지의 것입니다'
        }
    }
}

function Move-IntoPlace {
    <#
    NTFS 에서는 ReplaceFile 로 원자적으로 교체한다. 일부 외장/네트워크/9p
    파일 시스템은 ReplaceFile 을 지원하지 않으므로, 그 경우에만 기존 파일을
    잠시 옆으로 옮긴 뒤 교체하고 실패 시 즉시 되돌린다. 어느 경로에서도
    검증된 백업은 그대로 남는다.
    #>
    param([string]$Temp, [string]$Destination, [string]$SwapPath)

    if ([System.IO.File]::Exists($SwapPath) -or [System.IO.Directory]::Exists($SwapPath)) {
        Fail "예약된 임시 경로가 이미 있습니다: $SwapPath"
    }

    try {
        # PowerShell 은 [string] 매개변수에 $null 을 빈 문자열로 바인딩하므로
        # 백업 파일 없음을 뜻하는 null 은 [NullString]::Value 로 넘겨야 한다.
        [System.IO.File]::Replace($Temp, $Destination, [NullString]::Value)
        $script:ReplaceMode = 'atomic'
        return
    } catch {
        Write-Host '이 드라이브는 원자적 교체를 지원하지 않아 안전한 대체 경로로 교체합니다.'
    }

    [System.IO.File]::Move($Destination, $SwapPath)
    try {
        [System.IO.File]::Move($Temp, $Destination)
    } catch {
        # 복구에도 실패하면 유일한 원본일 수 있는 SwapPath를 보존한다.
        if (-not [System.IO.File]::Exists($Destination)) {
            [System.IO.File]::Move($SwapPath, $Destination)
        }
        throw
    }
    if (-not [System.IO.File]::Exists($Destination)) {
        Fail "교체 뒤 대상 파일을 찾을 수 없습니다: $Destination"
    }
    [System.IO.File]::Delete($SwapPath)
    $script:ReplaceMode = 'fallback'
}

function Get-GamePaths {
    param([string]$GameDirectory)
    $game = Assert-RealDirectory -Path $GameDirectory -Label '게임 폴더'
    $backupDir = Join-Path $game $script:BackupDirName
    return [pscustomobject]@{
        Game = $game
        Exe = Join-Path $game $script:GameExeName
        Pck = Join-Path $game $script:GamePckName
        BackupDir = $backupDir
        BackupPck = Join-Path $backupDir $script:BackupPckName
        State = Join-Path $backupDir $script:StateName
        InstallTemp = Join-Path $game $script:InstallTempName
        RestoreTemp = Join-Path $game $script:RestoreTempName
        SwapTemp = Join-Path $game $script:SwapTempName
    }
}

function Invoke-Install {
    param([string]$PackageDirectory, [string]$GameDirectory, [string]$ExpectedExeSha256, [string]$ExpectedPckSha256)

    $paths = Get-GamePaths -GameDirectory $GameDirectory
    [void](Assert-RegularFile -Path $paths.Exe -Label '게임 실행 파일')
    [void](Assert-RegularFile -Path $paths.Pck -Label '게임 PCK')
    Assert-FileNotLocked -Path $paths.Pck -Label '게임 PCK'
    if ([System.IO.Directory]::Exists($paths.BackupDir) -or [System.IO.File]::Exists($paths.BackupDir)) {
        Fail "이미 설치 또는 백업 상태가 있습니다: $($paths.BackupDir)"
    }
    foreach ($temporary in @($paths.InstallTemp, $paths.RestoreTemp, $paths.SwapTemp)) {
        if ([System.IO.File]::Exists($temporary) -or [System.IO.Directory]::Exists($temporary)) {
            Fail "예약된 임시 경로가 이미 있습니다: $temporary"
        }
    }

    Write-Host '패치 페이로드와 게임 빌드를 검사하는 중입니다. 잠시 기다려 주세요.'
    $audit = Invoke-PackageAudit -PackageDir $PackageDirectory -ExePath $paths.Exe -PckPath $paths.Pck `
        -ExpectedExeSha256 $ExpectedExeSha256 -ExpectedPckSha256 $ExpectedPckSha256

    $installTempOwned = $false
    [void][System.IO.Directory]::CreateDirectory($paths.BackupDir)
    try {
        [void](Assert-FileHash -Path $paths.Exe -Expected $ExpectedExeSha256 -Label '게임 실행 파일')
        [void](Assert-FileHash -Path $paths.Pck -Expected $ExpectedPckSha256 -Label '게임 PCK')

        Write-Host '원본 PCK를 백업하는 중입니다.'
        Write-NewFile -Path $paths.BackupPck -Bytes ([System.IO.File]::ReadAllBytes($paths.Pck))
        [void](Assert-FileHash -Path $paths.BackupPck -Expected $ExpectedPckSha256 -Label '백업된 원본 PCK')

        Write-CanonicalStateFile -Path $paths.State -ManifestSha256 $audit.ManifestSha256 `
            -SourcePckSha256 $ExpectedPckSha256 -LocalizedPckSha256 $audit.LocalizedSha256

        Write-Host '한국어 PCK를 생성하고 교체하는 중입니다.'
        Write-NewFile -Path $paths.InstallTemp -Bytes $audit.LocalizedBytes
        $installTempOwned = $true
        [void](Assert-FileHash -Path $paths.InstallTemp -Expected $audit.LocalizedSha256 -Label '생성된 한국어 PCK')
        Move-IntoPlace -Temp $paths.InstallTemp -Destination $paths.Pck -SwapPath $paths.SwapTemp
        $installTempOwned = $false
        [void](Assert-FileHash -Path $paths.Pck -Expected $audit.LocalizedSha256 -Label '설치된 한국어 PCK')
    } catch {
        if ($installTempOwned -and [System.IO.File]::Exists($paths.InstallTemp)) {
            [System.IO.File]::Delete($paths.InstallTemp)
        }
        # 교체가 시작되지 않았다면 이번 시도가 만든 경로만 정리한다.
        if ([System.IO.File]::Exists($paths.Pck) -and (Get-Sha256File -Path $paths.Pck) -ceq $ExpectedPckSha256) {
            if ([System.IO.File]::Exists($paths.State)) { [System.IO.File]::Delete($paths.State) }
            if ([System.IO.File]::Exists($paths.BackupPck)) { [System.IO.File]::Delete($paths.BackupPck) }
            try { [System.IO.Directory]::Delete($paths.BackupDir) } catch { }
        }
        throw
    }

    return [pscustomobject]@{
        result = 'INSTALLED'
        replace_mode = $script:ReplaceMode
        game_dir = $paths.Game
        source_pck_sha256 = $ExpectedPckSha256
        installed_pck_sha256 = $audit.LocalizedSha256
        manifest_sha256 = $audit.ManifestSha256
    }
}

function Invoke-Uninstall {
    param([string]$PackageDirectory, [string]$GameDirectory, [string]$ExpectedExeSha256, [string]$ExpectedPckSha256)

    $paths = Get-GamePaths -GameDirectory $GameDirectory
    [void](Assert-RegularFile -Path $paths.Exe -Label '게임 실행 파일')
    if (-not [System.IO.File]::Exists($paths.Pck) -and [System.IO.File]::Exists($paths.SwapTemp)) {
        # fallback가 destination을 옮긴 직후 중단된 경우, 완전한 설치 상태로
        # swap 소유권을 증명한 뒤 검증된 backup에서 원본을 복구한다.
        [void](Assert-RealDirectory -Path $paths.BackupDir -Label '백업 폴더')
        [void](Assert-RegularFile -Path $paths.BackupPck -Label '백업 PCK')
        [void](Assert-RegularFile -Path $paths.State -Label '설치 상태 파일')
        [void](Assert-RegularFile -Path $paths.SwapTemp -Label 'fallback swap PCK')
        $recoveryEntries = @([System.IO.Directory]::GetFileSystemEntries($paths.BackupDir) |
            ForEach-Object { [System.IO.Path]::GetFileName($_) } | Sort-Object)
        $recoveryExpected = @($script:BackupPckName, $script:StateName) | Sort-Object
        if (Compare-Object -ReferenceObject $recoveryExpected -DifferenceObject $recoveryEntries) {
            Fail '중단된 fallback 상태의 소유권을 검증할 수 없습니다'
        }
        $recoveryAudit = Invoke-PackageAudit -PackageDir $PackageDirectory -ExePath $paths.Exe `
            -PckPath $paths.BackupPck -ExpectedExeSha256 $ExpectedExeSha256 -ExpectedPckSha256 $ExpectedPckSha256
        $recoveryState = Read-JsonObject -Path $paths.State -Label '설치 상태 파일'
        Assert-InstallState -State $recoveryState -ManifestSha256 $recoveryAudit.ManifestSha256 `
            -SourcePckSha256 $ExpectedPckSha256 -LocalizedPckSha256 $recoveryAudit.LocalizedSha256
        $swapHash = Get-Sha256File -Path $paths.SwapTemp
        if ($swapHash -cne $ExpectedPckSha256 -and $swapHash -cne $recoveryAudit.LocalizedSha256) {
            Fail 'fallback swap PCK가 검증된 설치 상태와 일치하지 않습니다'
        }
        if ([System.IO.File]::Exists($paths.RestoreTemp) -or [System.IO.Directory]::Exists($paths.RestoreTemp)) {
            Fail "예약된 임시 경로가 이미 있습니다: $($paths.RestoreTemp)"
        }
        Write-NewFile -Path $paths.RestoreTemp -Bytes ([System.IO.File]::ReadAllBytes($paths.BackupPck))
        [System.IO.File]::Move($paths.RestoreTemp, $paths.Pck)
        [void](Assert-FileHash -Path $paths.Pck -Expected $ExpectedPckSha256 -Label '복구된 원본 PCK')
        [System.IO.File]::Delete($paths.SwapTemp)
    }
    [void](Assert-RegularFile -Path $paths.Pck -Label '설치된 게임 PCK')
    Assert-FileNotLocked -Path $paths.Pck -Label '설치된 게임 PCK'
    foreach ($temporary in @($paths.InstallTemp, $paths.RestoreTemp, $paths.SwapTemp)) {
        if ([System.IO.File]::Exists($temporary) -or [System.IO.Directory]::Exists($temporary)) {
            Fail "예약된 임시 경로가 이미 있습니다: $temporary"
        }
    }
    [void](Assert-RealDirectory -Path $paths.BackupDir -Label '백업 폴더')

    $entries = @([System.IO.Directory]::GetFileSystemEntries($paths.BackupDir) |
        ForEach-Object { [System.IO.Path]::GetFileName($_) } | Sort-Object)
    $expectedEntries = @($script:BackupPckName, $script:StateName) | Sort-Object
    $unexpected = @($entries | Where-Object { $_ -cne $script:BackupPckName -and $_ -cne $script:StateName })
    if ($entries.Count -eq 0 -or $unexpected.Count -ne 0) {
        Fail "백업 폴더에 예상하지 못한 항목이 있습니다: $($entries -join ', ')"
    }

    $current = Get-Sha256File -Path $paths.Pck
    if ($current -ceq $ExpectedPckSha256 -and $entries.Count -eq 1) {
        # 원본 복원 뒤 cleanup만 중단된 상태를 검증하고 마저 정리한다.
        $audit = Invoke-PackageAudit -PackageDir $PackageDirectory -ExePath $paths.Exe -PckPath $paths.Pck `
            -ExpectedExeSha256 $ExpectedExeSha256 -ExpectedPckSha256 $ExpectedPckSha256
        if ([System.IO.File]::Exists($paths.BackupPck)) {
            [void](Assert-RegularFile -Path $paths.BackupPck -Label '백업 PCK')
            [void](Assert-FileHash -Path $paths.BackupPck -Expected $ExpectedPckSha256 -Label '백업 PCK')
            [System.IO.File]::Delete($paths.BackupPck)
        } elseif ([System.IO.File]::Exists($paths.State)) {
            [void](Assert-RegularFile -Path $paths.State -Label '설치 상태 파일')
            $partialState = Read-JsonObject -Path $paths.State -Label '설치 상태 파일'
            Assert-InstallState -State $partialState -ManifestSha256 $audit.ManifestSha256 `
                -SourcePckSha256 $ExpectedPckSha256 -LocalizedPckSha256 $audit.LocalizedSha256
            [System.IO.File]::Delete($paths.State)
        }
        [System.IO.Directory]::Delete($paths.BackupDir)
        return [pscustomobject]@{
            result = 'UNINSTALLED'; replace_mode = 'none'; game_dir = $paths.Game
            restored_pck_sha256 = $ExpectedPckSha256; manifest_sha256 = $audit.ManifestSha256
        }
    }
    if (Compare-Object -ReferenceObject $expectedEntries -DifferenceObject $entries) {
        Fail "백업 폴더 상태가 불완전하며 원본 PCK가 복원되지 않았습니다: $($entries -join ', ')"
    }
    [void](Assert-RegularFile -Path $paths.BackupPck -Label '백업 PCK')
    [void](Assert-RegularFile -Path $paths.State -Label '설치 상태 파일')

    Write-Host '백업과 설치 상태를 검사하는 중입니다. 잠시 기다려 주세요.'
    $state = Read-JsonObject -Path $paths.State -Label '설치 상태 파일'
    $audit = Invoke-PackageAudit -PackageDir $PackageDirectory -ExePath $paths.Exe -PckPath $paths.BackupPck `
        -ExpectedExeSha256 $ExpectedExeSha256 -ExpectedPckSha256 $ExpectedPckSha256

    Assert-InstallState -State $state -ManifestSha256 $audit.ManifestSha256 `
        -SourcePckSha256 $ExpectedPckSha256 -LocalizedPckSha256 $audit.LocalizedSha256

    [void](Assert-FileHash -Path $paths.BackupPck -Expected $ExpectedPckSha256 -Label '백업 PCK')
    if ($current -cne $audit.LocalizedSha256 -and $current -cne $ExpectedPckSha256) {
        Fail '설치 후 PCK 가 변경됐습니다. 안전을 위해 자동 복원을 중단합니다'
    }

    if ($current -cne $ExpectedPckSha256) {
        Write-Host '원본 PCK를 복원하는 중입니다.'
        Write-NewFile -Path $paths.RestoreTemp -Bytes ([System.IO.File]::ReadAllBytes($paths.BackupPck))
        [void](Assert-FileHash -Path $paths.RestoreTemp -Expected $ExpectedPckSha256 -Label '복원용 원본 PCK')
        Move-IntoPlace -Temp $paths.RestoreTemp -Destination $paths.Pck -SwapPath $paths.SwapTemp
        [void](Assert-FileHash -Path $paths.Pck -Expected $ExpectedPckSha256 -Label '복원된 원본 PCK')
    }

    [System.IO.File]::Delete($paths.State)
    [System.IO.File]::Delete($paths.BackupPck)
    try {
        [System.IO.Directory]::Delete($paths.BackupDir)
    } catch {
        Fail "백업 폴더에 예상하지 못한 잔여 파일이 있습니다: $($paths.BackupDir)"
    }

    return [pscustomobject]@{
        result = 'UNINSTALLED'
        replace_mode = $script:ReplaceMode
        game_dir = $paths.Game
        restored_pck_sha256 = $ExpectedPckSha256
        manifest_sha256 = $audit.ManifestSha256
    }
}


function Resolve-GameDirectory {
    param([string]$Requested, [string]$PackageDirectory)

    if (-not [string]::IsNullOrWhiteSpace($Requested)) {
        return $Requested.Trim().Trim('"')
    }

    $package = Assert-RealDirectory -Path $PackageDirectory -Label '패치 폴더'
    $parent = [System.IO.Directory]::GetParent($package)
    if ($null -ne $parent) {
        $game = $parent.FullName
        if ([System.IO.File]::Exists((Join-Path $game $script:GameExeName)) -and
            [System.IO.File]::Exists((Join-Path $game $script:GamePckName))) {
            Write-Host "패치할 게임 폴더: $game"
            return $game
        }
    }
    Fail "패치 폴더 바로 위에서 drepo.exe와 drepo.pck를 찾지 못했습니다. 게임을 설치한 폴더 안에 패치 폴더를 통째로 넣은 뒤 install.cmd 또는 uninstall.cmd를 실행해 주세요. 현재 패치 폴더: $package"
}

function Invoke-Main {
    if ([string]::IsNullOrWhiteSpace($PackageDir)) {
        $package = $PSScriptRoot
    } else {
        $package = $PackageDir
    }
    if ([string]::IsNullOrWhiteSpace($package)) {
        Fail '패치 폴더를 결정할 수 없습니다. -PackageDir 로 지정해 주세요'
    }

    $expectedExe = if ([string]::IsNullOrWhiteSpace($ExpectedExeSha256)) { $script:TargetExeSha256 } else { $ExpectedExeSha256 }
    $expectedPck = if ([string]::IsNullOrWhiteSpace($ExpectedPckSha256)) { $script:TargetPckSha256 } else { $ExpectedPckSha256 }
    if (-not (Test-HexSha256 $expectedExe) -or -not (Test-HexSha256 $expectedPck)) {
        Fail '기대 SHA-256 핀 형식이 올바르지 않습니다'
    }

    $game = Resolve-GameDirectory -Requested $GameDir -PackageDirectory $package

    switch ($Command) {
        'install' {
            $result = Invoke-Install -PackageDirectory $package -GameDirectory $game `
                -ExpectedExeSha256 $expectedExe -ExpectedPckSha256 $expectedPck
        }
        'uninstall' {
            $result = Invoke-Uninstall -PackageDirectory $package -GameDirectory $game `
                -ExpectedExeSha256 $expectedExe -ExpectedPckSha256 $expectedPck
        }
        'audit' {
            $paths = Get-GamePaths -GameDirectory $game
            $audit = Invoke-PackageAudit -PackageDir $package -ExePath $paths.Exe -PckPath $paths.Pck `
                -ExpectedExeSha256 $expectedExe -ExpectedPckSha256 $expectedPck
            $result = [pscustomobject]@{
                result = 'AUDIT_OK'
                game_dir = $paths.Game
                manifest_sha256 = $audit.ManifestSha256
                localized_pck_sha256 = $audit.LocalizedSha256
            }
        }
    }

    Write-Host ''
    foreach ($property in $result.PSObject.Properties) {
        Write-Host "$($property.Name)=$($property.Value)"
    }
    Write-Host ''
    switch ($Command) {
        'install' { Write-Host '한국어 패치 설치가 완료되었습니다.' }
        'uninstall' { Write-Host '한국어 패치를 제거하고 원본을 복원했습니다.' }
        'audit' { Write-Host '패치 페이로드 검사를 통과했습니다.' }
    }
    return 0
}


try {
    $previousEncoding = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch { }
    try {
        $exitCode = Invoke-Main
    } finally {
        try { [Console]::OutputEncoding = $previousEncoding } catch { }
    }
    exit $exitCode
} catch {
    Write-Host ''
    Write-Host "실패: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host '게임 파일은 변경되지 않았거나 설치 전 상태로 되돌려졌습니다.'
    exit 1
}
