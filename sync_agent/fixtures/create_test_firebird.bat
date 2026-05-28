@echo off
REM ================================================================
REM  create_test_firebird.bat
REM
REM  Creates a local Firebird test database that mimics the POS schema.
REM  Run from the fixtures/ folder or right-click > Run as Administrator.
REM ================================================================

setlocal

SET ISQL="C:\Program Files\Firebird\Firebird_2_5\bin\isql.exe"
SET DB_DIR=C:\test_data
SET DB_FILE=%DB_DIR%\pdv_test.fdb
SET SQL_SCRIPT=%~dp0create_test_firebird.sql

echo.
echo === Creating test Firebird database ===
echo Target: %DB_FILE%
echo.

REM Confirm isql exists before proceeding
if not exist %ISQL% (
    echo ERROR: isql.exe not found at C:\Program Files\Firebird\Firebird_2_5\bin\
    echo Make sure Firebird 2.5 is installed.
    pause
    exit /b 1
)

REM Create target directory if needed
if not exist "%DB_DIR%" (
    mkdir "%DB_DIR%"
    echo Created directory %DB_DIR%
)

REM Remove existing database so the script always starts fresh
if exist "%DB_FILE%" (
    del /f /q "%DB_FILE%"
    echo Removed existing database
)

REM Run the SQL script — no database on the command line;
REM CREATE DATABASE inside the script handles the connection.
%ISQL% -u SYSDBA -p masterkey -i "%SQL_SCRIPT%"

if %errorlevel% == 0 (
    echo.
    echo === Done! ===
    echo Database created at %DB_FILE%
    echo.
    echo Update sync_agent/.env with:
    echo   FIREBIRD_DATABASE=C:\test_data\pdv_test.fdb
    echo.
    echo Verify with:
    echo   isql -u SYSDBA -p masterkey "C:\test_data\pdv_test.fdb"
    echo   SQL^> SELECT COUNT^(*^) FROM PRODUTO;
    echo   SQL^> EXIT;
) else (
    echo.
    echo ERROR: isql returned code %errorlevel%
    echo Check that Firebird 2.5 is installed and the password is masterkey.
)

echo.
pause
endlocal
