# Structure: associations, people, and doing everything from a command line

*Working design, first agreed 2026-11-13, and expected to change as it's built and used. Not built yet: the next feature work builds it, before `members` and `events`. Part of the [spec](spec.md), which points here from §4, §8, §9 and §11.*

Everything here is the same for every installation. An association's own structure is data in core's database, not code or config.

## 1. Units

Every installation is one tree of **units**. A unit is either an **association** or a **team**.

```
Example Federation              association, the root
├─ Extended board               team: seats filled by rule (§4)
├─ Spring hackathon 2027        team, a project: has an end date
└─ Example Robotics             association, inside the root
   ├─ Operations                team, no end date
   └─ Development               team, no end date: here its lead is Robotics' vice chair, by a seat rule
      └─ UAVs                   team inside a team
         └─ Drone race 2027     team, a project
```

- **The root is the installation's association,** and the legal entity. Most installations have no other associations: a root with teams under it.
- **A federation is an association with associations under it.** There's no separate kind. KTHIS, for example, is the root, with its societies under it.
- **Any depth is allowed.** Pages are designed for the usual two or three levels.
- **Projects and services are both teams.** A project has an end date and a service doesn't. When a project ends, it's archived: everyone's access through it ends, and its history is kept.
- **Teams can contain teams,** to any depth. That's how an association splits into departments, such as Operations and Development, which have teams of their own, which have projects.
- **Each unit has a fixed key,** such as `robotics`, unique in the installation, plus a name, a description, and its own logo and colours if it wants them. Renaming a unit never changes its key, so it never changes anyone's access.
- **A team can involve other associations.** It has one owning unit, which appoints its lead, and a list of associations taking part.
- **A service can be run for another association,** such as one association running another's website. The team sits under the association doing the work, with a "run for" link to the one it serves. Either side's head (§4) can end the arrangement.

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
| `Position` | person, unit, role, title, start, end, appointed by, minutes, seat rule | A board post or a role in a team (§4). Open until someone replaces the holder, as there are no fixed terms. A position that comes from a seat rule (§4) records which one |
| `Registration` | person, event, ticket, status, checked in | Signed up for an event (§6) |
| `Subscription` | person, unit, topic (events, news), include units below, confirmed, ended | Follows a unit's events or news (§6) |
| `Reason` | person, kind, unit, source, legal basis, expires | Why data is held on someone with no other tie: applicant, alumnus, partner contact, contact-form sender |

- **Membership is only of associations, and is inherited upwards.** A member of Robotics is a member of the root, for example to vote at its annual meeting, so joining once is enough. Someone can also be a member of the root directly, outside every association inside it. In teams, people hold positions instead.
- **Apps add and end ties and reasons,** and delete their own data when its time is up.
- **A person with no account, no current tie and no live reason is deleted** after a short grace period.
- **The record of who was involved is kept.** When someone is deleted, their ended positions stay, board and team alike, with their name and nothing else: no email and no other ties. So every unit keeps a full list of who led it and who took part, and when. It's kept as the association's records, which the privacy policy says, and it's the one exception to deletion. Plain memberships, sign-ups and subscriptions are deleted.
- **Non-members can take part in teams,** as a `participant`, such as a partner's employee or a student from elsewhere. Their position is their tie. They can never hold a board role or be a `lead`.
- **Alumni:** when a membership ends, the person is offered to stay as an alumnus, with their consent, as a `Reason` that's renewed every few years.

## 4. Roles and authority

A position's **role** is one of a fixed set of keys that the kit understands. Each association can rename their labels, and add its own titles.

| Role key | Typically held in | Can | Where |
|---|---|---|---|
| `chair`, `vice_chair` | association | Appoint and end positions. Create, change and archive units. Give grants (below). Everything the board can | The association and everything inside it, associations inside it included |
| `treasurer` | association | Fees and treasury, and everything a `board_member` can | The association and its teams only |
| `secretary` | association | Documents and minutes, and everything a `board_member` can | The association and its teams only |
| `board_member` | association | Manage memberships, events, mailings and documents, and see members | The association and its teams only |
| `lead` | team | Manage the team's participants, events and mailings. Appoint the leads of the teams under it, and approve proposals there | The team and the teams under it |
| `participant` | team | Take part, and get the team's access in the tools |
| custom title | either | Nothing beyond what its role key gives: a custom title sits on top of one of the keys above, such as "Head of PR" on `board_member` |

The keys never change and are never shown. What people see comes from the dictionary below.

- **Authority is scoped.** You can manage a unit if a position you hold covers it, as the table says, or a grant does. Only the chair and vice chair reach into the associations inside theirs. Every other board role stops at its association's edge. So the root's treasurer has no say in Robotics, unless a chair gives them one.
- **The head of a unit** is the `chair` or `vice_chair` of an association, or the `lead` of a team. Heads appoint the heads of the units directly under them and approve what's proposed there. So Development's lead appoints the lead of UAVs, and UAVs' lead approves a new project inside it. Anyone with authority higher up can do the same.
- **The root's chair** is recorded by the outgoing chair or vice chair, or by an operator on the server.
- **Operator isn't a role in the tree.** It's about running the server, so it stays a group given by hand, as now. It gives account recovery and server settings, and no say over members.
- **Two-factor authentication** is required for anyone holding a board role (`chair`, `vice_chair`, `treasurer`, `secretary`, `board_member`) anywhere, and for operators.

### Grants

A `chair` or `vice_chair` can **give a role a specific permission** anywhere they have authority themselves. For example, KTHIS's president could give:
- KTHIS's treasurer fees and treasury in every society;
- KTHIS's board members a view of every society's members, for the annual meeting;
- Robotics' Operations lead the right to send Robotics' mailings.

- **The permissions are a fixed list:** see members, manage memberships, manage events, send mailings, manage documents, and fees and treasury. Appointing people and giving grants can't be given away, so authority over people stays with chairs, vice chairs and heads.
- **A grant goes to a role in a unit, not to a person,** such as "KTHIS's treasurer". It passes to whoever holds that role next, and holders by a seat rule get it too.
- **It covers one unit,** or that unit and everything under it.
- **It's dated like a tie,** records who gave it, and lasts until a chair or vice chair ends it. It doesn't end when the person who gave it leaves.

### Seats that come with another position

A unit can have **seat rules:** holding one position gives you a seat in another unit, for as long as you hold the first one. For example:
- **On a federation's board:** the chair of each association directly under the root holds a `board_member` seat on the root;
- **An extended board:** a team whose participants are the root's board, plus the chair and vice chair of each association directly under the root;
- **A department led by the vice chair:** Robotics' vice chair is the `lead` of Development.

A rule names the role it gives here, the role it comes from, and where that role is held: one named unit, or every association directly under a named one.

- **Rules are optional,** and each unit sets its own, or none. They're for posts that statutes tie together. Otherwise, positions are appointed one by one: Development's lead can just as well be appointed directly.
- **A seat gives exactly its role, scoped as above.** Robotics' chair, seated on KTHIS's board, can do what a KTHIS `board_member` can in KTHIS and its teams, and nothing in the other societies.
- **A seat is a position like any other.** It gives the same access, needs 2FA if its role does, and stays in the record when it ends.
- **It starts and ends with the position it comes from,** so a new chair gets the board seat, and its rooms and documents, the moment they're appointed. It can't be set or ended by hand: change the rule, or the position behind it.
- **Rules are set by whoever can appoint in the unit** that has the seats.

### The dictionary

Every installation has its own **dictionary:** the word shown for each role key, unit kind and tie, in Swedish and English, singular and plural.

- **The kit ships plain words:** Chair, Vice chair, Treasurer, Secretary, Board member, Lead, Participant, Member, Association and Team. An installation can replace any of them: `sk words set treasurer "Head of Treasury" --sv "Kassör"`.
- **A unit can override words for itself alone,** not for the units below it. So a federation can call its own chair President while the associations inside it keep Chair: `sk words set chair "President" --unit kthis`.
- **It changes words only.** What a role can do comes from its key, so renaming `lead` to "Officer" changes nobody's access.
- **Custom titles** are still free text on a single position, such as "Head of PR", shown beside the word for its role.
- **The kinds of unit have words too,** such as Society for an association, or Committee for a team.

### Proposing a new unit

Anyone can propose a new team or association, through the `proposals` app planned beside recruitment. **It's approved one level up,** by the head of the unit it would sit under:
- a Robotics member proposes a Robotics project: Robotics' chair or vice chair approves it;
- someone proposes a new association under the root: the root's chair or vice chair approves it;
- someone proposes a project inside UAVs: UAVs' lead approves it.

A proposal suggests a lead or chair, who may be the proposer or someone else. Whoever approves it confirms that person or picks another. Approval creates the unit with them in position, and the tools catch up as for any other change.

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
- **Mailings have their own sending settings,** separate from the app's own mail. A server provider's relay is usually for notifications only: Hostup's, for example, excludes marketing mailings and is capped at 15,000 emails a month for all of a customer's servers together (checked 2026-11-13). So `mailings` sends through a provider the association chooses for it, preferably one in the EU, at the rate that provider allows. Without one set up, `mailings` is switched off, and confirmation emails for single sign-ups still go out as app mail.

## 7. One set of actions, three ways in

Every change, such as creating a team, appointing a lead or opening an event, is **one action in core:** a function with one permission check, the rule in §4. Every action is recorded in the audit log, with who did it and through which way in.

| Way in | For | Notes |
|---|---|---|
| **The API,** at `app.<domain>/api/v1/` | The CLI, scripts and CI | JSON over HTTPS. Versioned, so old CLIs keep working across a release |
| **Member pages** | Chairs, leads and members in a browser | Server-rendered, as §9 of the spec says |
| **The admin page** | Operators: a back office | Django's admin can only give permissions across the whole installation, so it's for operators, not for the board of an association inside another |

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
- **How core checks the CLI's tokens** (checked against django-allauth 65.21.1, which core runs, on 2026-11-13):
  - allauth's login service supports the device login, and comes with a check for its own tokens on an API built with django-ninja. That's the API library to use.
  - **Only the CLI's tokens may call the API.** The check only asks whether a token carries the right scope, so a tool's token, such as Forgejo's, would otherwise pass. The API needs its own scope, which only the CLI is allowed to ask for. Each token is also tied to `app.<domain>/api/`.
  - **The device approval page needs core's own guard,** as the tools' login page has: someone whose role needs 2FA and who hasn't set it up yet is sent to set it up first.
  - **The check doesn't look at whether the account is still active,** so the API does, on every call, along with the person's current ties.
  - **Token lifetimes are one setting for the whole installation,** and refresh tokens never expire unless set. The API sets a limit on both, and shortens it for roles that need 2FA by checking when the person last approved a login.
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

1. **Associations inside a federation as separate legal entities.** This design assumes the root is the only data controller. A federation whose associations are each their own legal entity would need a controller for each. Suggested: leave it out until an installation needs it.
2. **Which provider sends KTHIS's mailings,** and the volume to expect.
