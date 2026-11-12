# AI Gladiators: Competitions

The public definitions of AI Gladiators competitions: the targets, the rules of engagement, the scoring, and later the match results.

Everything a contestant faces is published here, in line with Kerckhoffs's principle: a target must stay secure when its full configuration is known. Anyone may read, run and attack these targets on their own systems.

## Layout

| Path | Contents |
|---|---|
| `targets/<family>/<name>/` | one target: its configuration, the client code that drives it, and a README |
| `competitions/<name>.md` | one competition: the target it uses, the rules of engagement, the scoring, and the claimed properties |
| `matches/` | published match results (later) |

The first target family is [Crossbar.io](https://crossbar.io), a WAMP router.

## License

- **Code and configuration** (router configs, client code, scripts): MIT, see [LICENSE](LICENSE).
- **Prose and results** (READMEs, competition rules, match results): Creative Commons Attribution 4.0 International (CC BY 4.0), see [LICENSE-docs](LICENSE-docs). Please credit "AI Gladiators" when you quote or reuse them.
