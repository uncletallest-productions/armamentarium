<!-- vendored from canon:NAMING.md sha256:1dd0da4b13e7854c3662b9fe4565b0c760d6541f61abd46ee6af1414918df8ea -- edit canon, not this file -->
# Naming: why everything here sounds like a cathedral library

Uncle Tallest Productions projects name things in Latin, with a deliberate nod to the
ecclesiastical, record-keeping High Gothic of Warhammer 40,000's Imperium: machine
shrines, reliquaries, archives, forges. It's a house style, not a dependency. Every
name below is ordinary Latin (or English), and nothing here needs you to know the
lore.

The names aren't only decoration, though. **A name tells you what kind of thing it is
and whose it is** before you read a word of documentation. This page is the key.

> Every repo that uses the scheme carries a copy of this file, kept identical to one
> canonical version. Improvements go into the canonical version and are copied out from
> there, so don't edit a copy in place.

---

## The rule: type-noun + qualifier

Storage names follow one pattern, **`<type-noun>-<qualifier>`**:

- The **type-noun** says what kind of store it is.
- The **qualifier**, usually a Latin genitive ("of the …"), says whose it is or what it holds.

| Type-noun | Meaning | Holds |
|---|---|---|
| *Librarium* | library | knowledge, notes, agent memory |
| *Scriptorium* | writing-room | writing |
| *Officina* | workshop | code and products |
| *Mechanicum* | machine-works | infrastructure, forges |
| *Archivum* | archive | backups, cold storage |

| Qualifier | Meaning |
|---|---|
| *Cogitatorum* | "of the thinkers" |
| *Scriptorum* | "of the writers" |
| *Fabricatorum* | "of the makers" |
| *Sanctorum* | "of the holy (places)" |
| *Imperialis* | "imperial" |
| *Veritas* | "truth" |

So `Officina-Scriptorum` is *the writers' workshop* (where the code lives),
`Librarium-Cogitatorum` is *the thinkers' library* (agent memory), and
`Mechanicum-Fabricatorum` is *the makers' machine-works* (the git forge).

Many datasets are mounted under a **short name** (the qualifier or a single noun) at
`~/Datasets/<Name>`. That's why paths read `~/Datasets/Profectionis/...` and not the full
pool name.

---

## Names you'll meet in these repos

### Storage and places

| Name | Latin | What it is |
|---|---|---|
| **Reliquary** | a container for relics | The home ZFS storage everything else is carved out of. |
| **Archivum** | archive | Backups and cold storage, kept separate from the working pool. |
| **Mobilus** | movable | A portable mirror of a curated subset of the Reliquary. |
| **Viaticus** | from *viaticum*, provisions for a journey | A laptop's local travel copy of the datasets it needs offline, replayed home on return. |
| **Horreum** | granary, storehouse | The offline package pool and provisioning manifests, used to rebuild a machine with no internet. |
| **Profectionis** | "of setting out" (*profectio*, departure) | The fleet-config store: what each machine is set up with, captured so it can be restored. |
| **Substrate** | (English) | The shared memory and working context of the AI agents. |
| **Cubicles** | from *cubiculum*, a private room | Per-role workspaces. Each holds a **Fabrica** (workshop) of repos. |
| **Scriptorum** | "of the writers" | The cubicle where the code and products are written. |
| **Gubernaculum** | helm, rudder | The operator's own cubicle: the hand on the tiller. |

### Tools and interfaces

| Name | Latin | What it is |
|---|---|---|
| **armamentarium** | armoury, arsenal; the complete kit for a task | This tool: what an agent is equipped with, under the operator's control. |
| **Stoa** | a covered colonnade (and the Stoics who met in one) | The desktop shell, a stable frame that everything passes through. Its **hubs** have their own glossary in the Stoa docs. |
| **Oraculum** | oracle | The Stoa hub for Claude Desktop sessions (built on armamentarium). |
| **Aerarium** | treasury | The Stoa hub for the dataset working set: what's mounted, and offline mode. |

### People and agents

| Name | What it means |
|---|---|
| **operator** | The human who owns the machines and grants access. |
| **cognate** | An AI agent working in the system: kin to the operator, not a tool and not a person. On remote hosts, it's also the name of the restricted user agents run as. |
| **offices** (Scriptor, Seneschal, …) | Cognates are named by the *seat* they hold, in the manner of SCA baronial offices, not by a personal name. *Scriptor* holds engineering. |

---

## Metaphors that belong elsewhere

Several naming systems coexist. Keep them apart:

- **Latin type-noun + qualifier:** storage and data (this page).
- **Ship and fleet** (crew, steerage, below-decks): Stoa's software tiers.
- **Greek underworld** (Hades, Styx, Erebos…): machines and network hosts.
- **Battlestar Galactica colonies:** internals of the window manager.

When a word could belong to two systems, the sentence around it decides. Erebos, for
example, is both a host name and a project name.

---

*The names are ordinary words, free for anyone. The particular set, used together for
these projects, is the house identity. See each repo's license and, where present,
`TRADEMARKS.md`.*
