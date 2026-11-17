<CodeBlock language="yaml" editable>
      - name: Validate Inno Setup script
        shell: pwsh
        run: |
          $script = "packaging/HashTools_Setup.iss"


if (!(Test-Path $script -PathType Leaf)) {
throw "Inno Setup script not found."
}

$content = Get-Content $script -Raw

if ($content.Contains("<CodeBlock") -or
$content.Contains("</CodeBlock>") -or
$content.Contains([string][char]96)) {
throw "Inno Setup script contains Markdown formatting."
}

$first = Get-Content $script |
Where-Object { $_.Trim() -ne "" } |
Select-Object -First 1

if ($first -notmatch '^\s*(#|;|\[)') {
throw "Invalid first line in Inno Setup script: $first"
}

foreach ($section in @("[Setup]", "[Files]", "[Icons]")) {
if (!$content.Contains($section)) {
throw "Missing Inno Setup section: $section"
}
}

Write-Host "Inno Setup validation passed."
</CodeBlock>