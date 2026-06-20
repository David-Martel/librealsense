# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$BuildDir,
    [ValidateSet('Release', 'Debug', 'RelWithDebInfo', 'MinSizeRel')]
    [string]$Configuration = 'Release',
    [ValidateSet('Ninja', 'Visual Studio 17 2022', 'Visual Studio 16 2019')]
    [string]$Generator = 'Ninja',
    [string]$Architecture = 'x64',
    [string]$CudaToolkitRoot,
    [string[]]$CudaArchitectures,
    [string]$PythonExecutable,
    [string]$ArtifactDir,
    [ValidateSet('none', 'sccache', 'ccache')]
    [string]$CompilerCache = 'none',
    [ValidateSet('msvc', 'clang-cl')]
    [string]$CompilerToolchain = 'msvc',
    [string]$VcpkgToolchainFile,
    [switch]$Clean,
    [switch]$RunCtest,
    [switch]$ValidateDevice
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-CMakePath {
    param([string]$Path)
    $Path.Replace('\', '/')
}

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$Arguments
    )
    Write-Host "$FilePath $($Arguments -join ' ')"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Find-CudaToolkitRoot {
    param([string]$RequestedRoot)
    if ($RequestedRoot -and (Test-Path -LiteralPath $RequestedRoot)) {
        return (Resolve-Path -LiteralPath $RequestedRoot).Path
    }
    $preferred = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.1'
    if (Test-Path -LiteralPath $preferred) {
        return $preferred
    }
    if ($env:CUDA_PATH -and (Test-Path -LiteralPath $env:CUDA_PATH)) {
        return (Resolve-Path -LiteralPath $env:CUDA_PATH).Path
    }
    $nvcc = Get-Command nvcc.exe -ErrorAction SilentlyContinue
    if ($nvcc) {
        return (Resolve-Path (Join-Path $nvcc.Source '..\..')).Path
    }
    throw 'CUDA toolkit root was not found. Pass -CudaToolkitRoot explicitly.'
}

function Find-CudaArchitectures {
    param([string[]]$RequestedArchitectures)
    if ($RequestedArchitectures -and $RequestedArchitectures.Count -gt 0) {
        return ($RequestedArchitectures -join ';')
    }
    $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if ($nvidiaSmi) {
        $caps = & $nvidiaSmi.Source --query-gpu=compute_cap --format=csv,noheader 2>$null |
            Where-Object { $_ -match '^\s*\d+(\.\d+)?\s*$' } |
            ForEach-Object { $_.Trim().Replace('.', '') } |
            Sort-Object -Unique
        if ($caps.Count -gt 0) {
            return ($caps -join ';')
        }
    }
    return '120;89'
}

function Find-PythonExecutable {
    param([string]$RequestedPython)
    if ($RequestedPython) {
        return (Resolve-Path -LiteralPath $RequestedPython).Path
    }
    $clariusPython = 'C:\codedev\clarius\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $clariusPython) {
        return $clariusPython
    }
    return (Get-Command python.exe -ErrorAction Stop).Source
}

function Find-VsDevCmd {
    $vswhere = Get-Command vswhere.exe -ErrorAction SilentlyContinue
    if ($vswhere) {
        $path = & $vswhere.Source -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find Common7\Tools\VsDevCmd.bat |
            Select-Object -First 1
        if ($path) {
            return $path
        }
    }
    @(
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat"
    ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

function Find-CompilerCache {
    param([string]$CacheName)
    if ($CacheName -eq 'none') {
        return $null
    }
    $cache = Get-Command "$CacheName.exe" -ErrorAction SilentlyContinue
    if (-not $cache) {
        throw "$CacheName.exe was not found on PATH"
    }
    return $cache.Source
}

function Find-ClangCl {
    $clangCl = Get-Command clang-cl.exe -ErrorAction SilentlyContinue
    if (-not $clangCl) {
        throw 'clang-cl.exe was not found on PATH'
    }
    return $clangCl.Source
}

function Find-VcpkgToolchainFile {
    param([string]$RequestedToolchainFile)
    if ($RequestedToolchainFile) {
        return (Resolve-Path -LiteralPath $RequestedToolchainFile).Path
    }
    if ($env:VCPKG_ROOT) {
        $candidate = Join-Path $env:VCPKG_ROOT 'scripts\buildsystems\vcpkg.cmake'
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $vcpkg = Get-Command vcpkg.exe -ErrorAction SilentlyContinue
    if ($vcpkg) {
        $candidate = Join-Path (Split-Path -Parent $vcpkg.Source) '..\scripts\buildsystems\vcpkg.cmake'
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Import-VsDevEnvironment {
    param([string]$Arch)
    $vsDevCmd = Find-VsDevCmd
    if (-not $vsDevCmd) {
        Write-Warning 'VsDevCmd.bat was not found. Ninja/MSVC builds may fail.'
        return
    }
    Write-Host "Loading Visual Studio build environment: $vsDevCmd"
    $scriptPath = Join-Path ([System.IO.Path]::GetTempPath()) ("lrs-vsdev-" + [guid]::NewGuid().ToString("N") + ".cmd")
    Set-Content -LiteralPath $scriptPath -Encoding ASCII -Value @(
        '@echo off',
        ('call "{0}" -arch={1} -host_arch={1} >nul' -f $vsDevCmd, $Arch),
        'set'
    )
    try {
        foreach ($line in (cmd.exe /d /c "`"$scriptPath`"")) {
            $idx = $line.IndexOf('=')
            if ($idx -gt 0) {
                [Environment]::SetEnvironmentVariable($line.Substring(0, $idx), $line.Substring($idx + 1), 'Process')
            }
        }
    }
    finally {
        Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    }
}

function Remove-BuildDirectorySafely {
    param(
        [string]$Repo,
        [string]$Build
    )
    $repoBuild = [System.IO.Path]::GetFullPath((Join-Path $Repo 'build'))
    $target = [System.IO.Path]::GetFullPath($Build)
    if (-not $target.StartsWith($repoBuild, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean build directory outside repo build root: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

$envSnapshot = @{
    Path = $env:Path
    PYTHONPATH = $env:PYTHONPATH
    CUDA_PATH = $env:CUDA_PATH
}

try {
    $RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
    if (-not $BuildDir) {
        $BuildDir = Join-Path $RepoRoot 'build\windows-cuda-py313-all'
    }
    $BuildDir = [System.IO.Path]::GetFullPath($BuildDir)
    if (-not $ArtifactDir) {
        $ArtifactDir = Join-Path $BuildDir '_artifacts'
    }
    $ArtifactDir = [System.IO.Path]::GetFullPath($ArtifactDir)

    if ($Clean) {
        Remove-BuildDirectorySafely -Repo $RepoRoot -Build $BuildDir
    }

    $selectedCudaRoot = Find-CudaToolkitRoot -RequestedRoot $CudaToolkitRoot
    $selectedCudaArchitectures = Find-CudaArchitectures -RequestedArchitectures $CudaArchitectures
    $selectedPython = Find-PythonExecutable -RequestedPython $PythonExecutable
    $selectedCompilerCache = Find-CompilerCache -CacheName $CompilerCache
    $selectedVcpkgToolchainFile = Find-VcpkgToolchainFile -RequestedToolchainFile $VcpkgToolchainFile

    $env:CUDA_PATH = $selectedCudaRoot
    $pathEntries = @(
        $BuildDir,
        (Join-Path $selectedCudaRoot 'bin'),
        (Join-Path $selectedCudaRoot 'bin\x64'),
        $envSnapshot.Path
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $env:Path = ($pathEntries -join ';')

    Import-VsDevEnvironment -Arch $Architecture
    New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null

    $cmakeDefinitions = @(
        "-DCMAKE_BUILD_TYPE=$Configuration",
        '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON',
        '-DBUILD_WITH_CUDA=ON',
        '-DBUILD_WITH_CPU_EXTENSIONS=ON',
        '-DBUILD_WITH_NEON=OFF',
        '-DBUILD_WITH_OPENMP=OFF',
        '-DRS2_GB10_USB_TUNING=OFF',
        '-DRS2_GB10_PC_ZEROCOPY=OFF',
        '-DRS2_GB10_CONV_CACHE=OFF',
        '-DFORCE_RSUSB_BACKEND=OFF',
        '-DBUILD_SHARED_LIBS=ON',
        '-DBUILD_WITH_STATIC_CRT=OFF',
        '-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL',
        '-DBUILD_EXAMPLES=ON',
        '-DBUILD_GRAPHICAL_EXAMPLES=ON',
        '-DBUILD_TOOLS=ON',
        '-DBUILD_PC_STITCHING=ON',
        '-DBUILD_PYTHON_BINDINGS=ON',
        '-DBUILD_CSHARP_BINDINGS=OFF',
        '-DBUILD_UNIT_TESTS=ON',
        '-DUNIT_TESTS_ARGS=--not-live',
        '-DCHECK_FOR_UPDATES=OFF',
        '-DENABLE_SECURITY_FLAGS=OFF',
        '-DBUILD_ASAN=OFF',
        '-DENABLE_CCACHE=OFF',
        "-DPYTHON_EXECUTABLE=$(ConvertTo-CMakePath $selectedPython)",
        "-DPython_EXECUTABLE=$(ConvertTo-CMakePath $selectedPython)",
        "-DPython3_EXECUTABLE=$(ConvertTo-CMakePath $selectedPython)",
        "-DCUDAToolkit_ROOT=$(ConvertTo-CMakePath $selectedCudaRoot)",
        "-DCUDA_TOOLKIT_ROOT_DIR=$(ConvertTo-CMakePath $selectedCudaRoot)",
        "-DCMAKE_CUDA_ARCHITECTURES=$selectedCudaArchitectures"
    )
    if ($CompilerToolchain -eq 'clang-cl') {
        $clangCl = ConvertTo-CMakePath (Find-ClangCl)
        $cmakeDefinitions += @("-DCMAKE_C_COMPILER=$clangCl", "-DCMAKE_CXX_COMPILER=$clangCl")
    }
    if ($selectedCompilerCache) {
        $cacheLauncher = ConvertTo-CMakePath $selectedCompilerCache
        $cmakeDefinitions += @(
            "-DCMAKE_C_COMPILER_LAUNCHER=$cacheLauncher",
            "-DCMAKE_CXX_COMPILER_LAUNCHER=$cacheLauncher",
            "-DCMAKE_CUDA_COMPILER_LAUNCHER=$cacheLauncher"
        )
    }
    if ($selectedVcpkgToolchainFile) {
        $cmakeDefinitions += "-DCMAKE_TOOLCHAIN_FILE=$(ConvertTo-CMakePath $selectedVcpkgToolchainFile)"
    }

    $configureArgs = @('-S', $RepoRoot, '-B', $BuildDir, '-G', $Generator)
    if ($Generator -like 'Visual Studio*') {
        $configureArgs += @('-A', $Architecture)
    }
    $configureArgs += $cmakeDefinitions
    Invoke-Checked -FilePath cmake -Arguments $configureArgs

    Invoke-Checked -FilePath cmake -Arguments @('--build', $BuildDir, '--config', $Configuration, '--target', 'all')

    if (Get-Command ninja.exe -ErrorAction SilentlyContinue) {
        & ninja -C $BuildDir -t targets all | Set-Content -LiteralPath (Join-Path $ArtifactDir 'targets.txt') -Encoding UTF8
    }
    & cmake -LAH -N $BuildDir | Set-Content -LiteralPath (Join-Path $ArtifactDir 'cmake-cache.txt') -Encoding UTF8

    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = if ($oldPythonPath) { "$BuildDir;$oldPythonPath" } else { $BuildDir }
    try {
        $importScript = @"
import json
import os
paths = [r"$BuildDir", r"$selectedCudaRoot\bin", r"$selectedCudaRoot\bin\x64"]
for path in paths:
    if os.path.isdir(path):
        os.add_dll_directory(path)
import pyrealsense2 as rs
print(json.dumps({
    "devices": rs.context().query_devices().size(),
    "file": rs.__file__,
    "has_gl": hasattr(rs, "gl"),
    "has_object_detection_frame": hasattr(rs, "object_detection_frame"),
    "version": getattr(rs, "__version__", None),
}, sort_keys=True))
"@
        & $selectedPython -c $importScript | Set-Content -LiteralPath (Join-Path $ArtifactDir 'pyrealsense2-import.json') -Encoding UTF8
        if ($LASTEXITCODE -ne 0) {
            throw "pyrealsense2 import validation failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:PYTHONPATH = $oldPythonPath
    }

    $benchmark = Get-ChildItem -LiteralPath $BuildDir -Recurse -Filter 'rs-benchmark.exe' | Select-Object -First 1
    if ($benchmark) {
        & $benchmark.FullName --version | Set-Content -LiteralPath (Join-Path $ArtifactDir 'rs-benchmark-version.txt') -Encoding UTF8
    }

    $enum = Get-ChildItem -LiteralPath $BuildDir -Recurse -Filter 'rs-enumerate-devices.exe' | Select-Object -First 1
    if ($enum) {
        $enumOutput = & $enum.FullName -s 2>&1
        $enumExit = $LASTEXITCODE
        [pscustomobject]@{ exit_code = $enumExit; output = @($enumOutput) } |
            ConvertTo-Json -Depth 4 |
            Set-Content -LiteralPath (Join-Path $ArtifactDir 'rs-enumerate-devices.json') -Encoding UTF8
        if ($ValidateDevice -and $enumExit -ne 0) {
            throw "rs-enumerate-devices failed with exit code $enumExit"
        }
    }

    if ($RunCtest) {
        Invoke-Checked -FilePath ctest -Arguments @('--test-dir', $BuildDir, '-C', $Configuration, '--output-on-failure', '--output-junit', (Join-Path $ArtifactDir 'ctest.xml'))
    }

    [pscustomobject]@{
        artifact_dir = $ArtifactDir
        build_dir = $BuildDir
        compiler_cache = $CompilerCache
        compiler_cache_path = $selectedCompilerCache
        compiler_toolchain = $CompilerToolchain
        configuration = $Configuration
        cuda_architectures = $selectedCudaArchitectures
        cuda_toolkit_root = $selectedCudaRoot
        generator = $Generator
        python_executable = $selectedPython
        repo_root = $RepoRoot
        run_ctest = [bool]$RunCtest
        vcpkg_toolchain_file = $selectedVcpkgToolchainFile
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ArtifactDir 'build-manifest.json') -Encoding UTF8
}
finally {
    $env:Path = $envSnapshot.Path
    $env:PYTHONPATH = $envSnapshot.PYTHONPATH
    $env:CUDA_PATH = $envSnapshot.CUDA_PATH
}
