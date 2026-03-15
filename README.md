# fedora-repository

This is my custom Fedora repository for hosting multiple services. None
of these services are built by me; they are simply downloaded from their
official sources and added to this repository so that they can be
installed through `dnf` and automatically receive updates.

I created this repository for my personal use. However, anyone is
welcome to use it if they find it useful.

If you prefer to host your own repository, I am also providing all the
scripts and explanations on how I created these repositories so they can
be replicated and self-hosted.

------------------------------------------------------------------------

## Current Active Repositories

1.  Citrix Workspace
2.  Rambox
3.  Bitwarden

``` bash
sudo dnf config-manager --add-repo https://fedora-repo.kozlev.com/citrix/citrix.repo
sudo dnf config-manager --add-repo https://fedora-repo.kozlev.com/rambox/rambox.repo
sudo dnf config-manager --add-repo https://fedora-repo.kozlev.com/bitwarden/bitwarden.repo

sudo dnf makecache
sudo dnf install ICAClient rambox bitwarden
```

------------------------------------------------------------------------

## 1. Principle of Work

### Install dependencies

``` bash
sudo dnf install createrepo httpd
```

### Create repository folder structure

``` bash
sudo mkdir -p /var/www/html/<repo>
```

### Start Apache server

``` bash
sudo systemctl enable --now httpd
```

------------------------------------------------------------------------

## 2. Helper Scripts

The helper scripts are located in the `root/<repo>` folder.

Each folder contains:

-   a **Python download script**
-   a **Bash update script**

------------------------------------------------------------------------

## 3. Cron Job

The Bash scripts are scheduled to run weekly via a cron job.

The purpose of the Bash scripts is to:

1.  Run the Python download scripts with the appropriate arguments
2.  Prune downloaded files to keep only the last **three versions**
3.  Sign the packages
4.  Update the repository metadata

### To set up the cron job

1.  Open the root crontab:

``` bash
sudo crontab -e
```

2.  Add the following entry for each service:

``` bash
0 0 * * 0 /bin/bash /root/<repo>/update-<repo>-repo.sh
```

------------------------------------------------------------------------

## 4. Signing the Packages

1.  Install dependencies

``` bash
sudo dnf install gnupg rpm-sign
```

2.  Generate a GPG key

``` bash
gpg --full-generate-key
```

3.  Recommended options:

-   Key type: `RSA`
-   Key size: `4096`
-   Expiration: `0` (no expiration)

4.  Configure RPM to use the key

``` bash
sudo micro ~/.rpmmacros
```

Add the following:

    %_signature gpg
    %_gpg_name <Name_of_repository>

5.  Sign the packages

``` bash
rpm --addsign /var/www/html/<repo>/*.rpm
```

6.  Export the public key

``` bash
gpg --export -a "<Repo-Name>" > /var/www/html/RPM-GPG-KEY-<repo>
```

------------------------------------------------------------------------

## Create Repo File

1.  Create the repo configuration file

``` bash
micro /var/www/html/<repo>.repo
```

Example configuration:

    [<repo-name>]
    name=(Kozlev's Repository) - <repo-name>
    baseurl=https://<url-to-repo>/<repo-name>/
    enabled=1
    gpgcheck=1
    gpgkey=https://<url-to-repo>/RPM-GPG-KEY-<repo-name>

------------------------------------------------------------------------

## Installation on Client Machine

1.  Download the repository file

``` bash
curl -o /etc/yum.repos.d/<repo-name>.repo <baseurl of repo>
```

2.  Refresh the repository cache

``` bash
sudo dnf makecache
```

3.  Install the package

``` bash
sudo dnf install <package-name>
```
