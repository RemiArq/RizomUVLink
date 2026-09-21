@echo off
rem Dev rebuild of rizomuv-mcp.exe, with the flags MakeMcpLauncher uses
rem (RizomUVApp/makefiledist.inc.php) in the environment makefiledist.php sets up:
rem VsDevCmd found by vswhere, the v142 toolset, Windows SDK 10.0.22621.0.
rem
rem   build.cmd [output dir]
rem
rem Output dir: the argument; else RizomUVApp\bin when this checkout sits in the RizomUV
rem tree (where the makefile puts the exe); else build\ next to this file. The version
rem resource takes the superproject's git describe, as rizomuv.exe does; 0.0.0 outside it.
rem
rem A running launcher (an MCP client using it) cannot be overwritten but can be renamed:
rem the old one is moved aside to rizomuv-mcp.exe.old and deleted on a later build.
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..\..\..\..") do set "SUPER=%%~fI"
set "INTREE="
if exist "%SUPER%\RizomUVApp\makefiledist.php" set "INTREE=1"

set "OUT=%~1"
if "%OUT%"=="" if defined INTREE set "OUT=%SUPER%\RizomUVApp\bin"
if "%OUT%"=="" set "OUT=%HERE%build"
for %%I in ("%OUT%") do set "OUT=%%~fI"

rem vswhere's path holds "(x86)", which closes any parenthesised block it is expanded in
rem (a for /f set included): hence the temp file rather than for /f
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto :no_vswhere
set "VS="
"%VSWHERE%" -products * -latest -property installationPath > "%TEMP%\rizomuv-mcp-vs.txt"
set /p VS=<"%TEMP%\rizomuv-mcp-vs.txt"
del "%TEMP%\rizomuv-mcp-vs.txt" 2>nul
if not defined VS goto :no_vs
rem VsDevCmd.bat calls vswhere by bare name and complains when it is not on PATH
set "PATH=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer;%PATH%"
call "%VS%\Common7\Tools\VsDevCmd.bat" -arch=amd64 -host_arch=amd64 -winsdk=10.0.22621.0 -vcvars_ver=14.2 >nul
if errorlevel 1 goto :no_env

set "VMAJ=0"
set "VMIN=0"
set "VREV=0"
rem "2027.0-501-ge478850d" -> 2027, 0, 501 (GetGitDescribe, docs/versioning.md)
if defined INTREE for /f "tokens=1-3 delims=.-" %%a in ('git -C "%SUPER%" describe --tags --long 2^>nul') do (set "VMAJ=%%a" & set "VMIN=%%b" & set "VREV=%%c")

set "OBJ=%TEMP%\rizomuv-mcp-launcher-obj"
if not exist "%OBJ%" mkdir "%OBJ%"
pushd "%OBJ%"
rc /nologo /DRIZOMUV_V_MAJOR=%VMAJ% /DRIZOMUV_V_MINOR=%VMIN% /DRIZOMUV_V_REV=%VREV% /fo rizomuv-mcp.res "%HERE%rizomuv-mcp.rc"
if errorlevel 1 goto :fail_build
cl /nologo /O1 /MT /W4 /WX /EHsc /DUNICODE /D_UNICODE "%HERE%rizomuv-mcp.cpp" rizomuv-mcp.res /Fe:rizomuv-mcp.exe /link /SUBSYSTEM:CONSOLE advapi32.lib
if errorlevel 1 goto :fail_build
popd

if not exist "%OUT%" mkdir "%OUT%"
if exist "%OUT%\rizomuv-mcp.exe.old" del "%OUT%\rizomuv-mcp.exe.old" 2>nul
copy /y "%OBJ%\rizomuv-mcp.exe" "%OUT%\rizomuv-mcp.exe" >nul 2>nul
if not errorlevel 1 goto :done
move /y "%OUT%\rizomuv-mcp.exe" "%OUT%\rizomuv-mcp.exe.old" >nul
if errorlevel 1 goto :fail_copy
copy /y "%OBJ%\rizomuv-mcp.exe" "%OUT%\rizomuv-mcp.exe" >nul
if errorlevel 1 goto :fail_copy

:done
echo build.cmd: %OUT%\rizomuv-mcp.exe, version %VMAJ%.%VMIN%.%VREV%
exit /b 0

:no_vswhere
echo build.cmd: vswhere not found: %VSWHERE% 1>&2
exit /b 1
:no_vs
echo build.cmd: vswhere found no Visual Studio installation 1>&2
exit /b 1
:no_env
echo build.cmd: VsDevCmd.bat failed in %VS% (is the v142 toolset, -vcvars_ver=14.2, installed?) 1>&2
exit /b 1
:fail_build
popd
echo build.cmd: build failed 1>&2
exit /b 1
:fail_copy
echo build.cmd: cannot write %OUT%\rizomuv-mcp.exe 1>&2
exit /b 1
