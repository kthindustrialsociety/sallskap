# Structure: associations, people, and doing everything from a command line

*Design, agreed 2026-11-13. Not built yet: the next feature work builds it, before `members` and `events`. Part of the [spec](spec.md), which points here from §4, §8, §9 and §11.*

Everything here is the same for every installation. An association's own structure is data in core's database, not code or config.

## 1. Units

Every installation is one tree of **units**. A unit is either an **association** or a **team**.

```
Example Society                 association, the root
├─ Website                      team, a service: no end date
├─ Spring hackathon 2027        team, a project: has an end date
└─ Example Robotics             association, a division of the root
   ├─ Competition 2027          team, a project
   └─ Example Robotics Junior   association, a division of a division
```

- **The root is the installation's association,** and the legal entity. Most installations have no other associations: a root with teams under it.
- **A federation is an association with associations under it.** There's no separate kind. KTHIS, for example, is the root, with its societies under it.
- **Any depth is allowed.** Pages are designed for the usual two or three levels.
- **Projects and services are both teams.** A project has an end date and a service doesn't. When a project ends, it's archived: everyone's access through it ends, and its history is kept.
- **Each unit has a fixed key,** such as `robotics`, unique in the installation, plus a name, a description, and its own logo and colours if it wants them. Renaming a unit never changes its key, so it never changes anyone's access.
- **A team can involve other associations.** It has one owning unit, which appoints its lead, and a list of associations taking part.
- **A service can be run for another association,** such as one association running another's website. The team sits under the association doing the work, with a "run for" link to the one it serves. Either side's chair can end the arrangement.

## 2. People

One `Person` row for **everyone the association holds data about:** members, board, people signed up for an event, subscribers, applicants, alumni and partner contacts.

- **Kept to a minimum:** name, language and email addresses. Emails are in their own table, each marked verified or not.
- **A login account is optional,** and linked to the person. People who only sign up for events or subscribe never need one. Today's accounts each become a person with an account, and keep the same `sub`, so the tools see no change.
- **Two rows are merged only on a verified email,** or by hand. An unverified email never joins someone to an existing person.
- **There's no general contact list.** People are seen through what ties them to a unit, so a chair sees their own members and the people signed up for their own events. One privacy role can search everyone, to answer GDPR requests.
- **Visitors aren't people in this model.** Page statistics come from the web server's logs, counted, and never linked to anyone.

## 3. Ties

What connects a person to a unit. Every tie has a start and an end. **Ending a tie sets its end date and never deletes it,** so "who led this in 2025" always has an answer.

| Tie | Fields | Meaning |
|---|---|---|
| `Membership` | person, association, kind (ordinary, honorary), start, end | A member of that association, **and of every association above it** |
| `Position` | person, unit, role, title, start, end, appointed by, minutes | A board post or a role in a team (§4). Open until someone replaces the holder, as there are no fixed terms |
| `Registration` | person, event, ticket, status, checked in | Signed up for an event (§6) |
| `Subscription` | person, unit, topic (events, news), include units below, confirmed, ended | Follows a unit's events or news (§6) |
| `Reason` | person, kind, unit, source, legal basis, expires | Why data is held on someone with no other tie: applicant, alumnus, partner contact, contact-form sender |

- **Membership is inherited upwards.** A member of a division is a member of the root, for example to vote at its annual meeting, so joining once is enough. Someone can also be a member of the root directly, outside every division.
- **Apps add and end ties and reasons,** and delete their own data when its time is up.
- **A person with no account, no current tie and no live reason is deleted** after a short grace period.
- **Alumni:** when a membership ends, the person is offered to stay as an alumnus, with their consent, as a `Reason` that's renewed every few years.

## 4. Roles and authority

A position's **role** is one of a fixed set of keys that the kit understands. Each association can rename their labels, and add its own titles.

| Role key | Typically held in | Can, in its unit and every unit below it |
|---|---|---|
| `chair`, `vice_chair` | association | Appoint and end positions. Create, change and archive units. Everything the board can |
| `treasurer` | association | Fees and treasury. Everything the board can |
| `secretary` | association | Documents and minutes. Everything the board can |
| `board_member` | association | Manage memberships, events, mailings and documents, and see members |
| `lead` | team | Manage the team's participants, events and mailings |
| `participant` | team | Take part, and get the team's access in the tools |
| custom title | either | Nothing beyond what its role key gives: a custom title sits on top of one of the keys above, such as "Head of PR" on `board_member` |

- **Authority flows down the tree.** You can manage a unit if your position in it, or in any unit above it, allows that action. A division's chair runs the division, and the root's board can step in anywhere.
- **The root's chair** is recorded by the outgoing chair or vice chair, or by an operator on the server.
- **Operator isn't a role in the tree.** It's about running the server, so it stays a group given by hand, as now. It gives account recovery and server settings, and no say over members.
- **Two-factor authentication** is required for anyone holding a board role (`chair`, `vice_chair`, `treasurer`, `secretary`, `board_member`) anywhere, and for operators.

## 5. Access to the tools

Access is **worked out from current memberships and positions,** never set by hand, apart from Operator. One function turns a person's ties into keys, and those keys go to the tools, in the login response and through the connectors, exactly as group keys do today (§8 of the spec).

- **Keys have the form `<unit>:<role>`,** such as `robotics:member`, `robotics:chair` or `website:lead`.
- **Today's fixed groups stay as keys,** so no module's mapping breaks: `member` is any membership, `board` is a board role on the root, `treasurer` is the root's treasurer, and `operator` is unchanged. `events` and `developer` become teams under the root.
- **Each module maps the tree** in its own way. In Forgejo, an association becomes an organisation and a team becomes a team, and the lead gets admin. In chat, an association becomes a space and each team gets rooms, with the lead as moderator.
- **The worker does the rest as now:** a change to a tie is saved with a note that the tools need telling. An archived project or an ended position removes access within seconds.

## 6. Events and mailings

**Events** are their own model, organised by one unit. They aren't teams: signing up is attendance, not work, and hundreds of sign-ups shouldn't be pushed to the tools. A large event such as a conference has a project team that organises it, and the event points at that team.

- **Signing up is as easy as on Luma:** name and email, with no account. A confirmation email verifies the address and links to a page for changing or cancelling the sign-up. Members-only events need a login.
- **A non-member's sign-up adds a `Reason`** that expires some months after the event, and the sign-up goes with it.
- **Following:** an event page and the site's widget offer "Follow <association>". That's a `Subscription` to the unit's events, and optionally to the units below it.

**Mailings** are their own app, `mailings`. A mailing goes to a unit's subscribers on a topic, or to its members for member business, such as an annual meeting notice. An event announcement is one kind of mailing and a newsletter another, so `events` never sends mail in bulk itself.

- **Every subscription is confirmed by email** before anything is sent to it. Every mailing has a one-click unsubscribe, in the message and in its headers.
- **Subscriptions live in core,** next to the person, not in `mailings`. That way unsubscribing, export and deletion cover every app at once.
- **Sending goes through the hosted provider** (§11 of the spec), at a rate it allows.

## 7. One set of actions, three ways in

Every change, such as creating a team, appointing a lead or opening an event, is **one action in core:** a function with one permission check, the rule in §4. Every action is recorded in the audit log, with who did it and through which way in.

| Way in | For | Notes |
|---|---|---|
| **The API,** at `app.<domain>/api/v1/` | The CLI, scripts and CI | JSON over HTTPS. Versioned, so old CLIs keep working across a release |
| **Member pages** | Chairs, leads and members in a browser | Server-rendered, as §9 of the spec says |
| **The admin page** | Operators: a back office | Django's admin can only give permissions across the whole installation, so it's for operators, not for a division's board |

There's no action that only one way in can do. A test checks that every action has its API endpoint and its check.

## 8. The CLI

Anything that can be done in core can be done from a terminal, by anyone, limited to what their positions allow.

```
sk login                                    # approve in the browser, with 2FA
sk whoami                                   # your positions, and what they let you do
sk unit create website --in robotics --kind team --lead anna@example.org
sk position set robotics chair erik@example.org
sk member add robotics anna@example.org     # sends an invite, never sets a password
sk event create robotics "Open house" --date 2027-02-04 --capacity 80
sk event registrations open-house --json
sk mailing send robotics news --file spring.md
```

`sk` is a placeholder. The name is chosen with the project's (§16 of the spec).

- **It runs on your own computer** and talks to the API over HTTPS, like `gh` talks to GitHub. Nobody gets a shell on the server, as §12 of the spec requires.
- **The server decides, never the CLI.** A changed CLI gets the same answer as an honest one: the action is refused. `sk --help` and `sk whoami` show only what you can do.
- **Login is the device login** that `gh auth login` uses (OAuth 2.0 device authorization, RFC 8628), through core's login service. It's switched on for the CLI alone, and stays off for every tool. You approve it in the browser, with your password and 2FA.
- **Tokens:**
  - They're kept in the operating system's keychain where there is one, and otherwise in a file only you can read.
  - They expire, and sooner for roles that need 2FA.
  - Ending someone's position or account ends their tokens' power at once, as every call is checked against current ties.
- **Confirming in the browser:** removing a person, ending a board position or deleting data asks you to approve it in the browser first, even with a valid token.
- **Tokens for scripts and CI** carry a subset of their creator's powers in one unit, and have an expiry. They're shown once, listed on your page, and stop working when the creator loses the position behind them.
- **Output:** readable by default, and `--json` gives output for scripts.
- **Distribution:** one Python file with only the standard library, attached to each release with a checksum, like `install.sh`.
- **The server commands stay in `kit`** for now: update, backup and restore, run as root on the server. The plan is one command for both once it has its final name. The server commands would then refuse to run anywhere but on the server, as root.

## 9. Open questions

1. **Non-members in teams.** Can someone who isn't a member hold a `participant` position, such as a partner's employee or a student from elsewhere? Suggested: yes, with a `Reason` of their own, but never a board role or `lead`.
2. **Old board positions after deletion.** Board posts are also recorded in the minutes. Suggested: keep a deleted person's ended board positions with their name, as part of the association's records, and delete every other tie.
3. **Divisions as separate legal entities.** This design assumes the root is the only data controller. A federation whose divisions are each their own legal entity would need a controller per division. Suggested: leave it out until an installation needs it.
4. **Sending in bulk.** Whether the hosted provider's limits and terms allow mailings at the expected volume, or whether mailings need a provider of their own.
5. **Who approves new teams and associations,** through the `proposals` app that's planned beside recruitment: the board of the unit above, or an annual meeting for an association.
6. **Protecting the API with core's login tokens.** Whether django-allauth's login service can check its own tokens on core's API directly, as its documentation suggests. To check before building, as was done for Vaultwarden.
