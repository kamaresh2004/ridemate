# Start the RideMate API against the local MongoDB demo database.
#
# The database name matters. The seeded demo accounts (rahul@ridemate.app,
# priya@ridemate.app, ... password "Password123") and their rides live in the
# `ridemate` database. Pointing the API at any other database name makes every
# seeded login fail with "Invalid email or password", because the account simply
# is not there.
#
#   .\start-local.ps1
#
# Frontend is served by this same process at http://localhost:5000
# Staff console (separate app): D:\Ridemate-captain-maintenance\start.cmd

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:MONGO_URI          = "mongodb://localhost:27017"
$env:MONGO_DB_NAME      = "ridemate"
$env:PAYMENT_PROVIDER   = "demo"
$env:FLASK_ENV          = "development"
$env:JWT_SECRET         = "local-dev-only-secret-0123456789abcdef"
$env:PORT               = "5000"

Set-Location $root
Write-Host "RideMate API -> http://localhost:5000  (MongoDB: $env:MONGO_URI / $env:MONGO_DB_NAME)" -ForegroundColor Cyan
& "$root\.venv\Scripts\python.exe" -u -m backend.app
