# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [string]$BuildDir,
    [ValidateSet('Release', 'Debug', 'RelWithDebInfo', 'MinSizeRel')]
    [string]$Configuration = 'Release',
    [ValidateSet('Visual Studio 17 2022', 'Visual Studio 16 2019', 'Ninja')]
    [string]$Generator = 'Visual Studio 17 2022',
    [string]$Architecture = 'x64',
    [switch]$Cuda,
    [string]$CudaToolkitRoot,
    [string[]]$CudaArchitectures,
    [switch]$BuildPythonBindings,
    [string]$PythonExecutable,
    [bool]$BuildExamples = $true,
    [bool]$BuildGraphicalExamples = $true,
    [bool]$BuildTools = $true,
    [string[]]$Targets,
    [switch]$NoBuild,
    [switch]$ValidateImport,
    [switch]$ValidateDevice
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-CMakeBool {
    param([bool]$Value)
    if ($Value) { 'ON' } else { 'OFF' }
}

function ConvertTo-CMakePath {
    param([string]$Path)
    $Path.Replace('\', '/')
}

function Find-CudaToolkitRoot {
    if ($CudaToolkitRoot) {
        return $CudaToolkitRoot
    }
    if ($env:CUDA_PATH -and (Test-Path -LiteralPath $env:CUDA_PATH)) {
        return $env:CUDA_PATH
    }
    $nvcc = Get-Command nvcc.exe -ErrorAction SilentlyContinue
    if ($nvcc) {
        return (Resolve-Path (Join-Path $nvcc.Source '..\..')).Path
    }
    return $null
}

function Find-CudaArchitectures {
    if ($CudaArchitectures -and $CudaArchitectures.Count -gt 0) {
        return ($CudaArchitectures -join ';')
    }

    $nvidiaSmi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    if (-not $nvidiaSmi) {
        return $null
    }

    $caps = & $nvidiaSmi.Source --query-gpu=compute_cap --format=csv,noheader 2>$null |
        Where-Object { $_ -match '^\s*\d+(\.\d+)?\s*$' } |
        ForEach-Object { $_.Trim().Replace('.', '') } |
        Sort-Object -Unique

    if ($caps.Count -eq 0) {
        return $null
    }
    return ($caps -join ';')
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

    $candidates = @(
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Insiders\Common7\Tools\VsDevCmd.bat"
    )

    return $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}

function Import-VsDevEnvironment {
    param([string]$Arch)

    $vsDevCmd = Find-VsDevCmd
    if (-not $vsDevCmd) {
        Write-Warning 'VsDevCmd.bat was not found. Visual Studio generator builds may still work, but Ninja/cl builds can fail.'
        return
    }

    Write-Host "Loading Visual Studio build environment: $vsDevCmd"
    $scriptPath = Join-Path ([System.IO.Path]::GetTempPath()) (
        "lrs-vsdev-" + [guid]::NewGuid().ToString("N") + ".cmd"
    )
    $lines = @(
        '@echo off',
        ('call "{0}" -arch={1} -host_arch={1} >nul' -f $vsDevCmd, $Arch),
        'set'
    )
    Set-Content -LiteralPath $scriptPath -Value $lines -Encoding ASCII
    try {
        $envLines = cmd.exe /d /c "`"$scriptPath`""
        foreach ($line in $envLines) {
            $idx = $line.IndexOf('=')
            if ($idx -le 0) {
                continue
            }
            [Environment]::SetEnvironmentVariable($line.Substring(0, $idx), $line.Substring($idx + 1), 'Process')
        }
    }
    finally {
        Remove-Item -LiteralPath $scriptPath -Force -ErrorAction SilentlyContinue
    }
}

$enableCuda = $Cuda.IsPresent
$enablePython = $BuildPythonBindings.IsPresent -or -not [string]::IsNullOrWhiteSpace($PythonExecutable)

if ($Targets -and $Targets.Count -gt 0) {
    $Targets = @(
        foreach ($target in $Targets) {
            $target -split ',' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        }
    )
}

if ($PythonExecutable) {
    $PythonExecutable = (Resolve-Path -LiteralPath $PythonExecutable).Path
} elseif ($enablePython) {
    $python = Get-Command python.exe -ErrorAction Stop
    $PythonExecutable = $python.Source
}

if (-not $BuildDir) {
    $suffix = if ($enableCuda) { 'cuda' } else { 'cpu' }
    if ($enablePython) {
        $suffix += '-python'
    }
    $BuildDir = Join-Path $RepoRoot "build\windows-$Architecture-$suffix"
}

Import-VsDevEnvironment -Arch $Architecture

$cmakeDefinitions = [System.Collections.Generic.List[string]]::new()
$cmakeDefinitions.Add("-DBUILD_WITH_CUDA=$(ConvertTo-CMakeBool $enableCuda)")
$cmakeDefinitions.Add('-DBUILD_WITH_CPU_EXTENSIONS=ON')
$cmakeDefinitions.Add('-DBUILD_WITH_NEON=OFF')
$cmakeDefinitions.Add('-DBUILD_WITH_OPENMP=OFF')
$cmakeDefinitions.Add('-DRS2_GB10_USB_TUNING=OFF')
$cmakeDefinitions.Add('-DRS2_GB10_PC_ZEROCOPY=OFF')
$cmakeDefinitions.Add('-DRS2_GB10_CONV_CACHE=OFF')
$cmakeDefinitions.Add('-DFORCE_RSUSB_BACKEND=OFF')
$cmakeDefinitions.Add('-DBUILD_SHARED_LIBS=ON')
$cmakeDefinitions.Add('-DBUILD_WITH_STATIC_CRT=OFF')
$cmakeDefinitions.Add('-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreadedDLL')
$cmakeDefinitions.Add("-DBUILD_EXAMPLES=$(ConvertTo-CMakeBool $BuildExamples)")
$cmakeDefinitions.Add("-DBUILD_GRAPHICAL_EXAMPLES=$(ConvertTo-CMakeBool $BuildGraphicalExamples)")
$cmakeDefinitions.Add("-DBUILD_TOOLS=$(ConvertTo-CMakeBool $BuildTools)")
$cmakeDefinitions.Add("-DBUILD_PYTHON_BINDINGS=$(ConvertTo-CMakeBool $enablePython)")
$cmakeDefinitions.Add('-DBUILD_CSHARP_BINDINGS=OFF')
$cmakeDefinitions.Add('-DBUILD_UNIT_TESTS=OFF')
$cmakeDefinitions.Add('-DCHECK_FOR_UPDATES=OFF')
$cmakeDefinitions.Add('-DENABLE_SECURITY_FLAGS=OFF')
$cmakeDefinitions.Add('-DBUILD_ASAN=OFF')
$cmakeDefinitions.Add('-DENABLE_CCACHE=OFF')

if ($enablePython) {
    $cmakePython = ConvertTo-CMakePath $PythonExecutable
    $cmakeDefinitions.Add("-DPYTHON_EXECUTABLE=$cmakePython")
    $cmakeDefinitions.Add("-DPython_EXECUTABLE=$cmakePython")
}

if ($enableCuda) {
    $detectedCudaRoot = Find-CudaToolkitRoot
    if ($detectedCudaRoot) {
        $cmakeCudaRoot = ConvertTo-CMakePath $detectedCudaRoot
        $cmakeDefinitions.Add("-DCUDAToolkit_ROOT=$cmakeCudaRoot")
        $cmakeDefinitions.Add("-DCUDA_TOOLKIT_ROOT_DIR=$cmakeCudaRoot")
    }

    $detectedArch = Find-CudaArchitectures
    if ($detectedArch) {
        $cmakeDefinitions.Add("-DCMAKE_CUDA_ARCHITECTURES=$detectedArch")
    }
}

$configureArgs = [System.Collections.Generic.List[string]]::new()
$configureArgs.AddRange([string[]]@('-S', $RepoRoot, '-B', $BuildDir, '-G', $Generator))
if ($Generator -like 'Visual Studio*') {
    $configureArgs.AddRange([string[]]@('-A', $Architecture))
} else {
    $configureArgs.Add("-DCMAKE_BUILD_TYPE=$Configuration")
}
$configureArgs.AddRange($cmakeDefinitions)

Write-Host 'Configuring librealsense for Windows...'
Write-Host "cmake $($configureArgs -join ' ')"
& cmake @configureArgs
if ($LASTEXITCODE -ne 0) {
    throw "CMake configure failed with exit code $LASTEXITCODE"
}

if ($NoBuild) {
    return
}

if (-not $Targets -or $Targets.Count -eq 0) {
    $Targets = @('realsense2')
    if ($BuildTools) {
        $Targets += 'rs-enumerate-devices'
    }
    if ($BuildExamples -and $BuildGraphicalExamples) {
        $Targets += @('rs-align', 'rs-pointcloud')
    }
    if ($enablePython) {
        $Targets += 'pyrealsense2'
    }
}

foreach ($target in $Targets) {
    $buildArgs = [System.Collections.Generic.List[string]]::new()
    $buildArgs.AddRange([string[]]@('--build', $BuildDir, '--config', $Configuration, '--target', $target))
    if ($Generator -like 'Visual Studio*') {
        $buildArgs.AddRange([string[]]@('--', '/m'))
    }
    Write-Host "Building target: $target"
    & cmake @buildArgs
    if ($LASTEXITCODE -ne 0) {
        throw "CMake build failed for target $target with exit code $LASTEXITCODE"
    }
}

if ($ValidateImport -and $enablePython) {
    $pyd = Get-ChildItem -LiteralPath $BuildDir -Recurse -Filter 'pyrealsense2*.pyd' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $pyd) {
        throw "pyrealsense2*.pyd was not found under $BuildDir"
    }

    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = if ($oldPythonPath) { "$($pyd.DirectoryName);$oldPythonPath" } else { $pyd.DirectoryName }
    try {
        & $PythonExecutable -c 'import pyrealsense2 as rs; print(rs.__file__); print(rs.context().query_devices().size())'
        if ($LASTEXITCODE -ne 0) {
            throw "pyrealsense2 import validation failed with exit code $LASTEXITCODE"
        }
    } finally {
        $env:PYTHONPATH = $oldPythonPath
    }
}

if ($ValidateDevice) {
    $enum = Get-ChildItem -LiteralPath $BuildDir -Recurse -Filter 'rs-enumerate-devices.exe' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $enum) {
        throw "rs-enumerate-devices.exe was not found under $BuildDir"
    }
    & $enum.FullName -s
    if ($LASTEXITCODE -ne 0) {
        throw "rs-enumerate-devices failed with exit code $LASTEXITCODE"
    }
}
