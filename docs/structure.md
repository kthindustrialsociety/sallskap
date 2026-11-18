# Structure: associations, people, and doing everything from a command line

*Working design, first agreed 2026-11-13, and expected to change as it's built and used. Being built, before `members` and `events`: units (§1), people (§2), memberships, positions and reasons (§3), authority, grants and seat rules (§4), keys to the tools (§5), and the actions with their audit log (§7) are in core, and the rest follows. Part of the [spec](spec.md), which points here from §4, §8, §9 and §11.*

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

- **The root is the installation's association,** and the legal entity. Most installations have no other associations: a root with teams under it. It's created from `name` and `key` under `[association]` in the config, follows the config's name, and can't be moved, archived or deleted.
- **A federation is an association with associations under it.** There's no separate kind. Example Federation, above, is one: the root, with Example Robotics under it.
- **Any depth is allowed.** Pages are designed for the usual two or three levels.
- **Projects and services are both teams.** A project has an end date and a service doesn't. When a project is over, someone archives it by hand, as projects rarely end exactly on their date: everyone's access through it ends, and its history is kept. Archiving a team archives everything under it. Taking a team out of the archive brings back that team alone, and only if the unit above it isn't archived.
- **Teams can contain teams,** to any depth. That's how an association splits into departments, such as Operations and Development, which have teams of their own, which have projects.
- **Each unit has a fixed key,** such as `robotics`, unique in the installation, plus a name, a description, and its own logo and colours if it wants them. Renaming a unit never changes its key, so it never changes anyone's access.
  - A key is lowercase letters, digits and single hyphens, starts with a letter, ends with a letter or digit, and is at most 40 characters, so every tool can use it as a name.
  - It can't be changed once set, and it's never reused, even after its unit is archived.
- **Nothing is created or moved under an archived unit,** an association sits only under an association, and a unit can't end up under itself.
- **A unit is deleted only if nothing ever pointed at it:** no units under it, and no ties (§3), grants or seat rules (§4), current or ended. Otherwise it's archived.
- **A team can involve other associations.** The unit it sits under owns it and appoints its lead. It also has a list of the other associations taking part.
- **A service can be run for another association,** such as one association running another's website. The team sits under the association doing the work, with a "run for" link to the one it serves. It's set up one level up, like a move (§4), and either side's chair or vice chair can end the arrangement.

## 2. People

One `Person` row for **everyone the association holds data about:** members, board, people signed up for an event, subscribers, applicants, alumni and partner contacts.

- **Kept to a minimum:** a name and email addresses, and nothing else. There's no language setting: the kit is in English. What an association asks of its members goes in member fields, on the membership (§3).
- **Emails are in their own table,** each marked verified or not. A verified address belongs to one person only. The same address can be on several people unverified, which is how a sign-up with a member's address gets its own row instead of joining theirs.
- **A login account is optional,** and linked to the person. People who only sign up for events or subscribe never need one. The account's login address is always one of its person's verified addresses. When it changes, the old one stays on the person unverified, so it can go to someone else, such as a role's shared address. Today's accounts each become a person with an account, and keep the same `sub`, so the tools see no change. Their addresses are marked verified, as they were typed in by an operator and already used to log in. An operator's typo becomes a verified address too, so check addresses when adding an account.
- **Two rows are merged only on a verified email,** or by hand, by an operator, which the audit log records (§7). An unverified email never joins someone to an existing person. When an address is verified and another person already holds it verified, the two become one: addresses, ties and reasons move over, and the emptied row is deleted. Two people who both have an account are never merged, as one of their `sub`s would vanish. If only one has an account, that's the person kept.
- **There's no general contact list.** People are seen through what ties them to a unit, so a chair sees their own members and the people signed up for their own events. One privacy role can search everyone, to answer GDPR requests.
- **Visitors aren't people in this model.** Page statistics come from the web server's logs, counted, and never linked to anyone.

## 3. Ties

What connects a person to a unit. Every tie has a start and an end. **Ending a tie sets its end date and never deletes it,** so "who led this in 2025" always has an answer.

| Tie | Fields | Meaning |
|---|---|---|
| `Membership` | person, association, kind (ordinary, honorary), start, end, member fields | A member of that association, **and of every association above it** |
| `Position` | person, unit, role, title, start, end, appointed by, minutes, seat rule, comes from | A board post or a role in a team (§4). Open until someone replaces the holder, as there are no fixed terms. A seat (§4) records the rule it comes from and the position that gives it. "Appointed by" and "minutes" are free text, such as "Annual meeting 2026" and where its minutes are |
| `Registration` | person, event, ticket, status, checked in | Signed up for an event (§6) |
| `Subscription` | person, unit, topic (events, news), include units below, confirmed, ended | Follows a unit's events or news (§6) |
| `Reason` | person, kind, unit, source, legal basis, start, expires | Why data is held on someone with no other tie: applicant, alumnus, partner contact, contact-form sender. The legal basis is consent, contract, legitimate interest or legal obligation (GDPR art. 6). It always expires, and ending it sets that date to today |

- **Dates, not times.** A tie's end is the first day it no longer holds, so a tie ended today stops today. A start can be in the future, as a board is often elected before it takes office. A person has at most one current membership of each association.

- **Membership is only of associations, and is inherited upwards.** A member of Robotics is a member of the root, for example to vote at its annual meeting, so joining once is enough. Someone can also be a member of the root directly, outside every association inside it. In teams, people hold positions instead. What someone is a member of is worked out from their memberships and the tree, never stored, so moving a unit or ending a membership can't leave it wrong.
- **Member fields:** what an association asks of its members beyond name and email, such as a programme, a graduation year or "member of the student union (yes/no)". Each association defines its own, as data: a name, a type (yes/no, text, number, or one of a list), whether it's required, and why it's held. The answers sit on the membership, so only those who can see that association's members see them, and they go with the membership. Built with `members`.
- **Apps add and end ties and reasons,** and delete their own data when its time is up.
- **A person with no account, no current tie and no live reason is deleted** after a short grace period.
- **The record of who was involved is kept.** When someone is deleted, their ended positions stay, board and team alike, with their name and nothing else: no email and no other ties. So every unit keeps a full list of who led it and who took part, and when. It's kept as the association's records, which the privacy policy says, and it's the one exception to deletion. Plain memberships, sign-ups and subscriptions are deleted.
- **Non-members can take part in teams,** as a `participant`, such as a partner's employee or a student from elsewhere. Their position is their tie. They can never hold a board role or be a `lead`: a board role in an association needs a membership of it, and a team's `lead` a membership of the association that owns the team or of one taking part in it, counting upwards in both cases. A membership can't end while it's what a board role or lead rests on: end or hand over the position first.
- **Board roles sit in associations,** and `lead` and `participant` in teams. An association has one `chair` at a time, and appointing the next ends the last one's position on the day the new one starts. Every other role can have several holders, such as two vice chairs or co-leads.
- **An advisory board is a team** whose advisors are participants, with "Advisor" as their title. They needn't be members, have no authority, and get the team's access in the tools.
- **Alumni:** when a membership ends, the person is offered to stay as an alumnus, with their consent, as a `Reason` that's renewed every few years.

## 4. Roles and authority

A position's **role** is one of a fixed set of keys that the kit understands. Each association can rename their labels, and add its own titles.

| Role key | Typically held in | Can | Where |
|---|---|---|---|
| `chair`, `vice_chair` | association | Appoint and end positions. Create, change and archive units. Give grants (below). Everything the board can | The association and everything inside it, associations inside it included |
| `treasurer` | association | Fees and treasury, and everything a `board_member` can | The association and its teams at any depth, stopping at the associations inside it |
| `secretary` | association | Documents and minutes, and everything a `board_member` can | The association and its teams at any depth, stopping at the associations inside it |
| `board_member` | association | Manage memberships, events, mailings and documents, and see members | The association and its teams at any depth, stopping at the associations inside it |
| `lead` | team | Manage the team's participants, events and mailings, change the team, and create teams inside it. Appoint the leads of the teams under it, and approve proposals there | The team and the teams under it |
| `participant` | team | Take part, and get the team's access in the tools |
| custom title | either | Nothing beyond what its role key gives: a custom title sits on top of one of the keys above, such as "Head of PR" on `board_member` |

The keys never change and are never shown. People see the kit's plain words for them, the same in every installation: Chair, Vice chair, Treasurer, Secretary, Board member, Lead and Participant, and for the rest Member, Association and Team. A custom title is shown beside the word for its role.

- **Authority is scoped.** You can manage a unit if a position you hold covers it, as the table says, or a grant does. Only the chair and vice chair reach into the associations inside theirs. Every other board role stops at its association's edge. So the root's treasurer has no say in Robotics, unless a chair gives them one.
- **Only what's current counts:** a position or grant (below) that has started and not yet ended, held in a unit that isn't archived. A board elected in spring has no say until its positions start.
- **The head of a unit** is the `chair` or `vice_chair` of an association, or the `lead` of a team. Heads appoint the heads of the units directly under them and approve what's proposed there. So Development's lead appoints the lead of UAVs, and UAVs' lead approves a new project inside it. Anyone with authority higher up can do the same.
  - A chair or vice chair appoints every role in their association and everything inside it, their own association's next chair included, and can end any of those positions.
  - A lead appoints the leads and participants of the teams under theirs, and their own team's participants. A co-lead of their own team is appointed by the head above, like the lead.
- **Moving, archiving and deleting a unit are decided one level up,** by whoever has authority over the unit above it, as for appointing its head. So a lead changes their own team and creates projects inside it, and the head above archives it. Bringing a team back from the archive and setting up a service for another association (§1) are decided there too. A move is decided by whoever has authority over the nearest unit above both its old and its new place. Creating a unit is decided by whoever has authority over the unit it goes into, which is how a lead approves a proposal there.
- **Associations taking part in a team, or served by it, get no say over it,** except that the chair or vice chair of the association it's run for can end that arrangement (§1).
- **The root's chair** is recorded by the outgoing chair or vice chair, or by an operator on the server with `kit chair <email>`, which the audit log records (§7). For a new installation, that's how the first chair is recorded: it also makes them a member of the root if they aren't one, as nobody else can yet.
- **Operator isn't a role in the tree.** It's about running the server, so it stays a group given by hand, as now. It gives account recovery and server settings, and no say over members: it never passes the check below. So the admin page shows the tree and changes none of it (§7).
- **Two-factor authentication** is required for anyone holding a board role (`chair`, `vice_chair`, `treasurer`, `secretary`, `board_member`) anywhere, and for operators. It's required from the day the position is recorded, even if it starts later, so a newly elected board sets it up before taking office.

### Permissions

Every action asks one question: **can this person do this, in this unit, today?** The answer comes from a fixed list of permissions, worked out from the person's current positions and the grants to them. Each one is checked on the unit the action is about, on the day it's done. So a board elected in spring is recorded by the board it replaces, and an appointment that starts later is checked when it's recorded, not when it starts.

| Permission | What it covers | Can be granted |
|---|---|---|
| `see_members` | See an association's members, or a team's participants | Yes |
| `manage_memberships` | Add, change and end memberships, in an association, and the reasons data is held on people there (§3) | Yes |
| `manage_events` | Events and their sign-ups | Yes |
| `send_mailings` | Mailings | Yes |
| `manage_documents` | Documents and minutes | Yes |
| `manage_finances` | Fees and treasury | Yes |
| `manage_participants` | Add and end participants, in a team | No |
| `appoint` | Start and end positions of any role, heads included | No |
| `manage_units` | Change this unit, and create, move, archive, bring back and delete the units inside it, and set them up as services for another association | No |
| `give_grants` | Give and end grants covering this unit | No |
| `end_service` | End a team's "run for" arrangement (§1) | No |

- **Chairs and vice chairs** have all of them. **Treasurers** have the six that can be granted, and **secretaries and board members** the same without fees and treasury, as minutes are documents. **Leads** have `see_members`, `manage_participants`, `manage_events`, `send_mailings` and `manage_units` in their team and the teams under it, and `appoint` in the teams under it only. **Participants** have none.
- **Seeing an association's members** means its own register. Robotics' members count as members of the root, but the root's board needs a grant to see them, as in the example below.

### Grants

A `chair` or `vice_chair` can **give a role a specific permission** anywhere they have authority themselves. For example, the root's chair could give:
- the root's treasurer fees and treasury in every association inside it;
- the root's board members a view of every association's members, for the annual meeting;
- Robotics' Operations lead the right to send Robotics' mailings.

- **Only the six permissions marked above can be granted:** see members, manage memberships, manage events, send mailings, manage documents, and fees and treasury. Appointing people, managing units and giving grants can't be given away, so authority over people stays with chairs, vice chairs and heads.
- **A grant goes to a role in a unit, not to a person,** such as "the root's treasurer". It passes to whoever holds that role next, and holders by a seat rule get it too. A role with several holders gives it to each of them, and a role nobody holds gives it to nobody until someone does. The role can be held anywhere: only the unit the grant covers has to be within the giver's authority.
- **It covers one unit,** or that unit and everything under it.
- **It's dated like a tie,** records who gave it, and lasts until a chair or vice chair ends it. It doesn't end when the person who gave it leaves. Whoever could give it can end it.
- **What it gives, to whom and where are fixed once given:** end it and give a new one. It's never deleted, so the record of who could do what is kept. The person who gave it must have been able to on the day it starts, and nothing new is given in an archived unit.

### Seats that come with another position

A unit can have **seat rules:** holding one position gives you a seat in another unit, for as long as you hold the first one. For example:
- **On a federation's board:** the chair of each association directly under the root holds a `board_member` seat on the root;
- **An extended board:** a team whose participants are the root's board, plus the chair and vice chair of each association directly under the root;
- **A department led by the vice chair:** Robotics' vice chair is the `lead` of Development.

A rule names the role it gives here, the roles it comes from, and where those are held: one named unit, or every association directly under a named one. So the extended board is two rules: one for the root's five board roles, and one for the chair and vice chair of each association directly under the root.

- **Rules are optional,** and each unit sets its own, or none. They're for posts that statutes tie together. Otherwise, positions are appointed one by one: Development's lead can just as well be appointed directly.
- **Rules are set by whoever can appoint in the unit** that has the seats, on the day the rule starts. Nothing new is set in an archived unit.
- **A rule is dated like a grant,** records who made it, and is never deleted, only ended, so the record of why someone held a seat is kept. The seat it gives and where it comes from are fixed once made: end it and make a new one. A rule that would give someone the same seat a current one gives them, for the same role held in the same place, is refused, and so is one giving a role from the same role in the same unit.
- **Any role that fits the unit can be a seat, except `chair`,** as a unit has one chair at a time and a rule can seat several people. A seat sits beside the positions appointed there and ends none of them: a lead by seat and an appointed lead are co-leads.
- **A seat that needs a membership comes only from a board role,** held in an association at or inside the one the seat needs: the seat's own association for a board role, or the association owning the team for a `lead` (§3). Every holder, now and later, is then already a member, so a rule in one unit never stops an appointment in another. A `participant` seat can come from any role. A move that would break this for a rule is refused.
- **Seats come only from appointed positions, never from other seats,** so there are no chains and no loops. The extended board above has Robotics' chair once, by the second rule, though they also hold a seat on the root's board.
- **A seat gives exactly its role, scoped as above.** Robotics' chair, seated on the root's board, can do what the root's `board_member` can in the root and its teams, and nothing in the other associations.
- **A seat is a position like any other.** It gives the same access, gets the grants to its role, needs 2FA if its role does, and stays in the record when it ends. It records its rule and the position it comes from.

**A seat runs from the later of the rule's start and the start of the position it comes from, to the earlier of their ends,** and follows when either one's dates change. It can't be set, changed or ended by hand: change the rule, or the position behind it.

- **A new chair gets the board seat,** and its rooms and documents, the moment they're appointed, and the last chair's seat ends with their position. A position recorded with a future start gets its seat at once, with the same start, so a board seat needs 2FA from that day too.
- **A rule made when holders already exist** seats them from its own start, never earlier. Ending a rule ends its seats on the same day.
- **When an association moves in under a rule's named unit,** its holders' seats start that day, and when it moves out, they end. If it moves back later, new seats start that day, so the gap stays in the record. A new association's holders get theirs as they're appointed.
- **A position in a team that's archived gives no seat** from the day it's archived, and a new one from the day the team is brought back, as a project that's over gives nothing. Seats in an archived team stay there like any other position there, giving nothing, and no new ones are added.
- **A seat that would never start isn't made,** and one already recorded that no longer would ends on its start day, as positions are never deleted.

### Proposing a new unit

Anyone can propose a new team or association, through the `proposals` app planned beside recruitment. **It's approved one level up,** by the head of the unit it would sit under:
- a Robotics member proposes a Robotics project: Robotics' chair or vice chair approves it;
- someone proposes a new association under the root: the root's chair or vice chair approves it;
- someone proposes a project inside UAVs: UAVs' lead approves it.

A proposal suggests a lead or chair, who may be the proposer or someone else. Whoever approves it confirms that person or picks another. Approval creates the unit with them in position, and the tools catch up as for any other change.

## 5. Access to the tools

Access is **worked out from current memberships and positions,** never set by hand, apart from Operator. One function turns a person's ties into keys, and those keys go to the tools, in the login response and through the connectors (§8 of the spec). Both get the same keys.

- **A position gives `<unit>:<role>`,** such as `robotics:chair`, `website:lead` or `uavs:participant`. A seat is a position, so it gives the same key as an appointed one. A lead doesn't also get `participant`, and positions don't count upwards.
- **A membership gives `<association>:member`** for its association and every association above it, as membership counts upwards (§3). So a member of Robotics gets `robotics:member` and the root's.
- **Only what's current counts,** as for authority (§4): a tie that has started and not yet ended, in a unit that isn't archived. So a board elected in spring gets its keys the day its positions start, and an archived project's people lose theirs.
- **Nothing else gives keys.** Grants are permissions in core, not access to the tools. A team run for another association, or one other associations take part in, gives those associations nothing: only the team's own positions do.
- **Four keys are the same in every installation,** so a module can name them without knowing the tree: `member` is any membership, `board` a board role on the root, seats included, `treasurer` the root's treasurer, and `operator` the Operator group. Operator is the one group, given by hand, and there are no others.
- **Keys from roles that need two-factor authentication** (board roles anywhere, `board`, `treasurer` and `operator`) only reach the tools once the person has set it up.
- **Each module maps the tree** in its own way, and its README says how. In Forgejo, the root's association is the installation's organisation and every other association gets its own. A team becomes a team that its people write to and its leads administer. Each organisation has a team for its members, which can read a repo once someone opens it to them, and its association's chair and vice chair own it. In chat, an association becomes a space and each team gets rooms, with the lead as moderator.
- **The worker does the rest as now:** a change to a tie, a seat or the tree is saved with a note that the tools need telling, for everyone it touches. An archived project or an ended position removes access within seconds. A tie that starts or ends on a day with no change, such as a board taking office on 1 July, is picked up just after midnight.

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

Every change, such as creating a team, appointing a lead or opening an event, is **one action in core:** a function with one permission check, the rule in §4. Every action is recorded in the audit log, with who did it and through which way in. Units, ties, grants and seat rules change only through actions, and a test checks that nothing else in core writes them.

| Way in | For | Notes |
|---|---|---|
| **The API,** at `app.<domain>/api/v1/` | The CLI, scripts and CI | JSON over HTTPS. Versioned, so old CLIs keep working across a release |
| **Member pages** | Chairs, leads and members in a browser | Server-rendered, as §9 of the spec says |
| **The admin page** | Operators: a back office | Accounts, two-factor authentication, follow-ups, and merging two people. It shows the tree, people, ties, grants, seat rules and the audit log, and changes none of them, as operators never pass the check in §4 |

There's no action that only one way in can do. A test checks that every action has its API endpoint and its check. Two commands on the server sit outside the actions, as they're about running the server: `kit admin`, which makes someone an operator, and `kit chair`, which records the root's chair (§4).

### The audit log

- **One row for each action,** saying when, which action, through which way in, who did it (their account and person), the unit the check asked about, what changed, and each field it set, before and after.
- **Operators' own changes are recorded too:** accounts, Operator, removing someone's two-factor authentication, follow-ups and merges, and `kit admin` and `kit chair` on the server. So are the changes core makes itself, such as merging two people when an address is verified, or deleting people with nothing left (§3), which are counted. Seats aren't recorded on their own: the action that started or ended them is the record, and they follow from their rule and the position they come from.
- **It names nobody.** People are known by an id, never by name or address. A tie's row leads to its person through the tie, so once a membership or a reason is deleted (§3), nothing in the log leads to them. A reason's source, and an account's name and address, are recorded as changed, without their values.
- **It's only ever added to.** Nothing in core changes or deletes a row, and the admin page lets nobody do so, operators included. Someone with root on the server can still change the database, as with everything else there.
- **Operators read it** in the admin page. A refused attempt changes nothing, so it isn't recorded.

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
