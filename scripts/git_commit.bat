@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
set "LESSCHARSET=utf-8"

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_DIR=%%~fI"

pushd "%REPO_DIR%" >nul 2>&1
if errorlevel 1 (
    echo Failed to enter repo directory: "%REPO_DIR%"
    exit /b 1
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo Current directory is not a Git repository: "%REPO_DIR%"
    popd
    exit /b 1
)

set "COMMIT_MSG=%*"

if "%COMMIT_MSG%"=="" (
    set /p COMMIT_MSG=Enter commit message: 
)

if "%COMMIT_MSG%"=="" (
    echo Commit message cannot be empty.
    popd
    exit /b 1
)

echo.
echo Repository: "%REPO_DIR%"
echo Commit message: %COMMIT_MSG%
echo.
echo Current status:
git -c core.quotePath=false status --short --branch
if errorlevel 1 (
    echo Failed to read Git status.
    popd
    exit /b 1
)

echo.
set /p CONFIRM=Continue with git add -A and git commit? [Y/N]: 
if /I not "%CONFIRM%"=="Y" (
    if /I not "%CONFIRM%"=="YES" (
        echo Cancelled.
        popd
        exit /b 0
    )
)

git add -A
if errorlevel 1 (
    echo git add failed.
    popd
    exit /b 1
)

git diff --cached --quiet
if not errorlevel 1 (
    echo No staged changes to commit.
    popd
    exit /b 0
)

git commit -m "%COMMIT_MSG%"
if errorlevel 1 (
    echo git commit failed.
    popd
    exit /b 1
)

echo.
echo Commit completed successfully.
git -c core.quotePath=false status --short --branch

popd
exit /b 0
