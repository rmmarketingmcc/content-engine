-- Content Engine Photos: finds photos in the Mac's Photos library and exports copies for the content engine's photo
-- picker (scripts/content_engine_photos.py --library). Read-only towards Photos: it only searches, counts and exports
-- copies; it never edits, moves or deletes anything. All logic lives in Python. Rebuilding this app makes macOS ask for
-- its permission again, so it takes general asks and otherwise stays unchanged. content_engine_photos.py --install-app
-- compiles it with __WORK_DIR__ set to <cache_dir>/photos/.
-- In:  __WORK_DIR__request.txt
--        line 1: default window in days back (only photos newer than this)
--        then one line per ask: <kind><TAB><arg><TAB><max>[<TAB><newer than days><TAB><older than days>]
--        kind = search (Photos' search field), album (an album's name) or recent (every photo in the window);
--        max 0 = only count; the two optional bounds replace the default window (older than 0 = up to today)
-- Out: export/<n>/<copy>.jpeg, done, manifest.tsv:
--        "#" lines: kind:arg, matches, matches in the window, newest match at all, newest match in the window
--        numbered lines: n, kind:arg, id, filename, date taken;  "!" lines: errors
-- Newest first within the window. Variable names are long on purpose: short ones like "kind" are Photos' own terms.
script listStore
	property foundItems : {}
	property itemDates : {}
	property windowIndexes : {}
end script

on isoText(someDate)
	if someDate is missing value then return ""
	return (someDate as «class isot») as string
end isoText

on run
	set workDir to "__WORK_DIR__"
	try
		set askLines to paragraphs of (read (POSIX file (workDir & "request.txt")) as «class utf8»)
		set defaultNewerDays to (item 1 of askLines) as integer
		set manifestText to ""
		set seenIds to {}
		set exportCount to 0
		set photosWasRunning to application "Photos" is running
		tell application "Photos"
			with timeout of 1800 seconds
				repeat with lineIndex from 2 to count of askLines
					set askLine to item lineIndex of askLines
					if askLine contains tab then
						set AppleScript's text item delimiters to tab
						set askFields to text items of askLine
						set AppleScript's text item delimiters to ""
						set askKind to item 1 of askFields
						set askArg to item 2 of askFields
						set askMax to (item 3 of askFields) as integer
						set newerDays to defaultNewerDays
						set olderDays to 0
						if (count of askFields) ≥ 5 then
							set newerDays to (item 4 of askFields) as integer
							set olderDays to (item 5 of askFields) as integer
						end if
						set oldestAllowed to (current date) - newerDays * days
						set newestAllowed to (current date) - olderDays * days
						try
							set foundItems of listStore to {}
							set itemDates of listStore to {}
							if askKind is "search" then
								set foundItems of listStore to search for askArg
								set scanMax to count of foundItems of listStore
								if scanMax > 8000 then set scanMax to 8000
								repeat with scanIndex from 1 to scanMax
									set end of itemDates of listStore to date of item scanIndex of foundItems of listStore
								end repeat
							else if askKind is "album" then
								set foundItems of listStore to every media item of album askArg
								set itemDates of listStore to date of every media item of album askArg
							else if askKind is "recent" then
								set foundItems of listStore to every media item
								set itemDates of listStore to date of every media item
							end if
							set windowIndexes of listStore to {}
							set newestAny to missing value
							set newestInWindow to missing value
							repeat with scanIndex from 1 to count of itemDates of listStore
								set oneDate to item scanIndex of itemDates of listStore
								if oneDate is not missing value then
									if newestAny is missing value then
										set newestAny to oneDate
									else if oneDate > newestAny then
										set newestAny to oneDate
									end if
									if oneDate > oldestAllowed and oneDate ≤ newestAllowed then
										set end of windowIndexes of listStore to scanIndex
										if newestInWindow is missing value then
											set newestInWindow to oneDate
										else if oneDate > newestInWindow then
											set newestInWindow to oneDate
										end if
									end if
								end if
							end repeat
							set windowCount to count of windowIndexes of listStore
							set manifestText to manifestText & "#" & tab & askKind & ":" & askArg & tab & (count of foundItems of listStore) & tab & windowCount & tab & (my isoText(newestAny)) & tab & (my isoText(newestInWindow)) & linefeed
							set keptCount to 0
							set failCount to 0
							set usedIndexes to {}
							repeat while keptCount < askMax
								set bestIndex to 0
								repeat with windowPos from 1 to windowCount
									set scanIndex to item windowPos of windowIndexes of listStore
									if usedIndexes does not contain scanIndex then
										if bestIndex = 0 then
											set bestIndex to scanIndex
										else if item scanIndex of itemDates of listStore > item bestIndex of itemDates of listStore then
											set bestIndex to scanIndex
										end if
									end if
								end repeat
								if bestIndex = 0 then exit repeat
								set end of usedIndexes to bestIndex
								set pickedItem to item bestIndex of foundItems of listStore
								try
									set pickedId to id of pickedItem
									if seenIds does not contain pickedId then
										set end of seenIds to pickedId
										set exportCount to exportCount + 1
										set exportDir to workDir & "export/" & exportCount & "/"
										do shell script "mkdir -p " & quoted form of exportDir
										with timeout of 900 seconds
											export {pickedItem} to (POSIX file exportDir) without using originals
										end timeout
										set keptCount to keptCount + 1
										set manifestText to manifestText & exportCount & tab & askKind & ":" & askArg & tab & pickedId & tab & (filename of pickedItem) & tab & (my isoText(item bestIndex of itemDates of listStore)) & linefeed
									end if
								on error exportError number exportErrorNumber
									set manifestText to manifestText & "!" & tab & askKind & ":" & askArg & tab & exportErrorNumber & " " & exportError & linefeed
									set failCount to failCount + 1
									if failCount ≥ 3 then exit repeat
								end try
							end repeat
						on error askError number askErrorNumber
							set manifestText to manifestText & "!" & tab & askKind & ":" & askArg & tab & askErrorNumber & " " & askError & linefeed
						end try
					end if
				end repeat
			end timeout
			if not photosWasRunning then quit
		end tell
		set manifestFile to open for access (POSIX file (workDir & "manifest.tsv")) with write permission
		set eof of manifestFile to 0
		write manifestText to manifestFile as «class utf8»
		close access manifestFile
	on error runError number runErrorNumber
		do shell script "printf '%s' " & quoted form of ((runErrorNumber as string) & " " & runError) & " > " & quoted form of (workDir & "error.txt")
	end try
	do shell script "touch " & quoted form of (workDir & "done")
end run
