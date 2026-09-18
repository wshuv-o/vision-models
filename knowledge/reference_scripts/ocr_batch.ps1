param([Parameter(Mandatory=$true)][string]$Dir)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($task, $type) {
  $m = $asTaskGeneric.MakeGenericMethod($type)
  $t = $m.Invoke($null, @($task)); $t.Wait(-1) | Out-Null; $t.Result
}
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime] | Out-Null
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { Write-Error "No OCR engine"; exit 2 }

$Dir = [System.IO.Path]::GetFullPath($Dir.Replace("/","\"))
$pngs = Get-ChildItem -Path $Dir -Filter *.png | Sort-Object Name
$n = 0
foreach ($png in $pngs) {
  $outJson = [System.IO.Path]::ChangeExtension($png.FullName, ".json")
  if (Test-Path $outJson) { $n++; continue }
  $file    = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($png.FullName)) ([Windows.Storage.StorageFile])
  $stream  = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $result  = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $words = New-Object System.Collections.ArrayList
  foreach ($line in $result.Lines) { foreach ($w in $line.Words) {
    $r = $w.BoundingRect
    [void]$words.Add([pscustomobject]@{ t=$w.Text; x=[math]::Round($r.X,1); y=[math]::Round($r.Y,1); w=[math]::Round($r.Width,1); h=[math]::Round($r.Height,1) }) } }
  [System.IO.File]::WriteAllText($outJson, ($words | ConvertTo-Json -Compress -Depth 3), [System.Text.Encoding]::UTF8)
  $bitmap.Dispose(); $stream.Dispose()
  $n++
  if ($n % 10 -eq 0) { Write-Host "ocr $n/$($pngs.Count)" }
}
Write-Host "DONE $n"
