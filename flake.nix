{
  description = "Cloudflare email transport for Odoo 18 — mail_cloudflare addon + Email Worker";

  inputs = {
    # odoo/ (OCB) is a git submodule; expose its contents to the flake source
    # tree so odoo-nix can derive addons_path from it and checks.odoo-tests can
    # run odoo-bin straight out of the store copy.
    self.submodules = true;
    odoo-nix.url = "github:Avunu/odoo-nix";
    nixpkgs.follows = "odoo-nix/nixpkgs";
  };

  nixConfig = {
    extra-substituters = [ "https://devenv.cachix.org" ];
    extra-trusted-public-keys = [
      "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw="
    ];
  };

  outputs =
    { self, odoo-nix, ... }@inputs:
    odoo-nix.lib.mkFlake { inherit inputs; } (
      { ... }:
      {
        imports = [ odoo-nix.flakeModules.default ];

        systems = [
          "aarch64-darwin"
          "aarch64-linux"
          "x86_64-darwin"
          "x86_64-linux"
        ];

        perSystem =
          {
            config,
            pkgs,
            lib,
            system,
            ...
          }:
          let
            # The repo root doubles as the custom-addons dir (layout.customDir
            # = "."), so the module lives at ./mail_cloudflare and consumers can
            # mount the repo itself under modules/. Everything that lints or
            # hooks over the source must therefore be handed a *filtered* tree:
            # odoo/ is a 2 GB OCB checkout and worker/ is TypeScript with its
            # own toolchain, and neither belongs in a ruff or git-hooks sandbox.
            moduleSrc = lib.cleanSourceWith {
              name = "odoo-cloudflare-email-src";
              src = self;
              filter =
                path: _type:
                let
                  rel = lib.removePrefix (toString self + "/") (toString path);
                in
                !(lib.elem rel [
                  "odoo"
                  "worker"
                ]);
            };

            # Same synthesis odoo-nix uses for odoo.conf, re-rooted on the store
            # copy of the flake for the sandboxed test run (no dev_mailcatch:
            # the suite mocks the transport and never opens a real connection).
            # The layout is read back from the option set below so the two can
            # never drift apart.
            addons = odoo-nix.lib.addons {
              inherit lib;
              inherit (config.odoo-nix) layout;
              workspaceRoot = ./.;
            };

            ruffFiles = "^mail_cloudflare/.*\\.py$";
          in
          {
            odoo-nix = {
              enable = true;
              projectName = "odoo-cloudflare-email";
              workspaceRoot = ./.;
              odooSeries = "18.0";
              python = pkgs.python311;

              # The repo root *is* the custom-addons dir: addons.nix emits "./."
              # and oca_sources.py picks up ./mail_cloudflare as the editable uv
              # source. Side effect: packages.builtOdoo (which copies customDir
              # into an assembled tree) is meaningless for this repo — the
              # module ships via the `addons` branch / consumer submodules, not
              # as a deployable Odoo tree. Never build or deploy it from here.
              layout.customDir = ".";

              odooConf = {
                dbName = "mail_cloudflare";
                # millrun's dev stack usually runs on this machine (5432 /
                # 8069 / 8072 / 1025 / 8025); keep every listener distinct so
                # both stacks can be up at once.
                dbPort = 5433;
                httpPort = 8169;
                geventPort = 8172;
                withoutDemo = true;
              };
              mailcatch = {
                port = 1026;
                httpPort = 8125;
              };

              # With customDir = "." the reload watcher would recurse over OCB
              # and worker/node_modules; edits are picked up by restarting the
              # odoo process instead. (watchdog is also kept out of the dev
              # dependency group for the same reason.)
              dev.autoReload = false;

              # ruff from nixpkgs, not the uv dev group: the flake check, the
              # pre-push hook and the shell then agree on one binary. Node
              # already comes with odoo-nix's shell (cfg.nodejs).
              extraDevPackages = [ pkgs.ruff ];
            };

            # Local pre-push quality gates via devenv's built-in git-hooks
            # module (installs a `pre-push` hook with prek on shell entry).
            # pre-push, not pre-commit: the worker gate needs worker/node_modules,
            # which only exists locally, and ruff over the module is cheap
            # enough to run once per push. CI covers the same ground via
            # checks.ruff and the worker workflow.
            devenv.shells.default = {
              git-hooks = {
                # The `run` derivation (checks.pre-commit) does `git add .` on
                # this tree — keep OCB and the worker out of it.
                rootSrc = lib.mkForce moduleSrc;
                hooks = {
                  ruff = {
                    enable = true;
                    package = pkgs.ruff;
                    # The built-in default adds --fix; a pre-push gate must
                    # only report.
                    entry = "${pkgs.ruff}/bin/ruff check";
                    files = ruffFiles;
                    stages = [ "pre-push" ];
                  };
                  ruff-format = {
                    enable = true;
                    package = pkgs.ruff;
                    entry = "${pkgs.ruff}/bin/ruff format --check";
                    files = ruffFiles;
                    stages = [ "pre-push" ];
                  };
                  worker-check = {
                    enable = true;
                    name = "worker checks (oxfmt + oxlint + tsc)";
                    entry = "${config.odoo-nix.nodejs}/bin/npm --prefix worker run check";
                    files = "^worker/.*\\.(ts|json|jsonc)$";
                    pass_filenames = false;
                    stages = [ "pre-push" ];
                  };
                };
              };
            };

            checks =
              {
                # ruff over the module only; pyproject.toml's [tool.ruff] is
                # read from the filtered tree's root. --no-cache: no writable
                # HOME in the sandbox.
                ruff =
                  pkgs.runCommandLocal "odoo-cloudflare-email-ruff" { nativeBuildInputs = [ pkgs.ruff ]; }
                    ''
                      cd ${moduleSrc}
                      ruff check --no-cache mail_cloudflare
                      ruff format --check --no-cache mail_cloudflare
                      touch $out
                    '';

                # Validates the git-hooks config; the pre-push hooks themselves
                # are skipped in the sandbox (same as wordpress-jwt-auth).
                pre-commit = config.devenv.shells.default.git-hooks.run;
              }
              // lib.optionalAttrs (lib.hasSuffix "-linux" system) {
                # Linux only: postgresqlTestHook is broken on darwin and only
                # the Linux sandbox gives a loopback-only network, which the
                # HttpCase tests need (Odoo 18 starts the HTTP server under
                # --test-enable even with --stop-after-init).
                odoo-tests = pkgs.stdenvNoCC.mkDerivation {
                  name = "odoo-cloudflare-email-odoo-tests";
                  # No src/unpack: odoo-bin runs straight from the flake's store
                  # copy (2 GB with OCB — copying it into the build dir would
                  # dominate the run). Python skips __pycache__ writes on the
                  # read-only store silently.
                  dontUnpack = true;
                  dontConfigure = true;
                  dontBuild = true;
                  doCheck = true;
                  nativeCheckInputs = [
                    pkgs.postgresql_16
                    pkgs.postgresqlTestHook
                    # The dev env carries freezegun + websocket-client, which
                    # odoo.tests imports unconditionally.
                    config.packages.odooDevEnv
                  ];
                  # The role must be able to create the test database and
                  # (like odoo-nix's dev role) install extensions.
                  postgresqlTestUserOptions = "LOGIN SUPERUSER CREATEDB";
                  checkPhase = ''
                    runHook preCheck
                    export HOME="$TMPDIR"
                    set -o pipefail
                    python ${self}/odoo/odoo-bin \
                      -d "$PGDATABASE" --db_host="$PGHOST" --db_user="$PGUSER" \
                      --addons-path="${addons.addonsPathFor "${self}"}" \
                      --data-dir="$TMPDIR/odoo-data" --http-port=18069 \
                      -i mail_cloudflare --test-enable --test-tags /mail_cloudflare \
                      --stop-after-init --without-demo=all --log-level=test \
                      2>&1 | tee odoo-tests.log
                    # Belt and braces over the exit code: Odoo logs test
                    # failures at ERROR and a typo'd --test-tags selects
                    # nothing while still exiting 0.
                    if grep -E ' (ERROR|CRITICAL) |FAIL(ED)?:' odoo-tests.log; then
                      echo "odoo-tests: errors in the log above" >&2
                      exit 1
                    fi
                    grep -E 'mail_cloudflare: [0-9]+ tests' odoo-tests.log \
                      || { echo "odoo-tests: no mail_cloudflare tests ran" >&2; exit 1; }
                    runHook postCheck
                  '';
                  installPhase = ''
                    mkdir -p $out
                    cp odoo-tests.log $out/
                  '';
                };
              };
          };
      }
    );
}
