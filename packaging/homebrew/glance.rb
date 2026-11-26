# Homebrew formula for the Btocode/homebrew-glance tap:
#
#   brew tap Btocode/glance
#   brew install glance
#   glance setup            # once per user: LaunchAgent, harper-ls, glance doctor
#
# Copy this file to Formula/glance.rb in github.com/Btocode/homebrew-glance. For a new
# release, update url and sha256 (the sha256 is in the release's SHA256SUMS) and, when
# scripts/core-wheels.sha256 changes, the glance_core resources.
#
# Python packages come from PyPI at install time (pip in the formula's own virtualenv); that
# is fine for a tap, not for homebrew-core.
class Glance < Formula
  desc "System-wide writing assistant: grammar, tone rewrites and AI actions in any app"
  homepage "https://github.com/Btocode/glance"
  url "https://github.com/Btocode/glance/releases/download/v0.4.0/glance-0.4.0.tar.gz"
  sha256 "REPLACE_WITH_SHA256_FROM_SHA256SUMS"
  license "MIT"

  depends_on :macos
  depends_on macos: :ventura
  depends_on "python@3.12"

  # Harper in Glance's own process (pinned abi3 wheels from the core-v0.3.0 release)
  resource "glance_core" do
    on_arm do
      url "https://github.com/Btocode/glance/releases/download/core-v0.3.0/glance_core-0.3.0-cp310-abi3-macosx_11_0_arm64.whl"
      sha256 "d7afa4d5c7e8d9f06eebb407a70004eb101561fe3cf0747192de0f17a1d75509"
    end
    on_intel do
      url "https://github.com/Btocode/glance/releases/download/core-v0.3.0/glance_core-0.3.0-cp310-abi3-macosx_10_12_x86_64.whl"
      sha256 "3dc56677f294343d355720bc86b89cff809aa649c2dee0198435408e64869143"
    end
  end

  def install
    python = Formula["python@3.12"].opt_bin/"python3.12"
    system python, "-m", "venv", libexec # with pip: the dependencies come from PyPI
    system libexec/"bin/python", "-m", "pip", "install", "--no-cache-dir", "#{buildpath}[macos]"
    resource("glance_core").stage do
      system libexec/"bin/python", "-m", "pip", "install", "--no-deps", Dir["*.whl"].first
    end
    %w[glance glanced glance-capture glance-edit].each do |cmd|
      bin.install_symlink libexec/"bin"/cmd
    end

    # What `glance setup` runs (scripts/install-macos.sh --setup-only) and reads
    (share/"glance").install "install.sh", "uninstall.sh", "pyproject.toml", "scripts",
                             "packaging", "extension"

    # `glance update` points at brew instead of the curl installer
    site_packages = libexec/Language::Python.site_packages(python)
    (site_packages/"glance/_packaged").write "Homebrew|brew upgrade glance\n"
  end

  def caveats
    <<~EOS
      Glance runs per user. Once, run:
        glance setup
      It installs a LaunchAgent (io.github.btocode.glance) that starts Glance at login,
      fetches the grammar engine and checks the setup with glance doctor.

      macOS then asks for Accessibility: allow the Python that runs Glance under
      System Settings > Privacy & Security > Accessibility. Glance reads and writes only
      the focused text field, and never password fields.

      Before `brew uninstall glance`, run:
        glance setup --undo
    EOS
  end

  test do
    assert_match "Glance #{version}", shell_output("#{bin}/glance --version")
  end
end
