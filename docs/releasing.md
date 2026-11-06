# Releasing

Versions are semantic: `MAJOR.MINOR.PATCH`. They stay at 0.x until another association can install and update the kit on its own, which is 1.0. The minor number counts feature releases, and the patch number counts fixes.

`VERSION` holds the number of the newest release. It only changes in a release pull request, so a commit on `main` after a release reads as that release plus the commit, such as `0.6.0+abc1234`.

## Steps

1. **A release pull request** changes `VERSION` and adds a section at the top of `CHANGELOG.md`, headed `## <version> (<date>)`:
   - **Before you update,** written by hand: a step to take by hand, a breaking change, a longer pause than usual, a tool's upgrade guide to read first. If there's nothing, it says "Nothing."
   - **Tool versions,** from the image lines that changed in the Compose files.
   - **Changes,** one line per pull request since the last release, starting from their titles.
2. **CI checks** that `VERSION` has a section, that every section has a "Before you update" part, and that versions go down from the top.
3. **Merge it**, then tag the merge commit and publish the release, with the section as its notes:

   ```sh
   gh release create v<version> --target <merge commit> --notes-file <the section>
   ```

## Rules

- **A tag never moves.** A ruleset stops anyone, owners included, from moving or deleting a `v*` tag. A bad tag is fixed with the next patch release.
- **A published section is never edited.** A mistake is put right in the next release's section.
- **`kit update` refuses a tag whose `VERSION` doesn't match its name,** so a tag on the wrong commit can't be installed.
