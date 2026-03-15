# fedora-repository
This is my custom fedora repository for hosting multiple services. None of these services are build by me, they are simple downloeaded from official sources and added to this repo so that they can be installed through dnf and allowed to auto update

I created this repository for my personal use, however everyone is welcome to use it themselves if they want to.
If not, I'm providing all scripts and explanation on how I created the repos so that they can be replicated and self hosted.

### Current Active Repositories
1. Citrix Workspace
2. Rambox 
3. Bitwarden

```
curl -o /etc/yum.repos.d/citrix.repo https://fedora-repo.kozlev.com/citrix.repo 
curl -o /etc/yum.repos.d/rambox.repo https://fedora-repo.kozlev.com/rambox.repo
curl -o /etc/yum.repos.d/bitwarden.repo https://fedora-repo.kozlev.com/bitwarden.repo
sudo dnf makecache
sudo dnf install ICAClient rambox bitwarden
```

### 1. Principle of Work
**Install Dependencies**

`sudo dnf install createrepo httpd`

**Create repository folder structure**

`sudo mkdir -p /var/www/html/<repo>`

**Start Apache server**

`sudo systemctl enable --now httpd`

-----------
### 2. Helper scripts

The helper scripts are all located in the `root/<repo>` folder.
Each folder contains both the python _download_ script and the _update_ bash script. 

-----------
### 3. Cron Job
The bash scripts are scheduled to run every week via a cron job.
The purpose of the bash scripts is to:
1. run the python download scripts, passing the appropriate arguments
2. prune the downloaded files to keep only the last 3 versions
3. sign the packages
4. update the repository metadata.

#### To set up the cron job:
1. `sudo crontab -e`
2. enter the following entry for each service:

    `0 0 * * 0 /bin/bash /root/<repo>/update-<repo>-repo.sh`
-----------
### 4. Signing the packages
1. install dependencies `sudo dnf install gnupg rpm-sign`
2. run `gpg --full-generate-key`
3. choose `Key type=RSA, size=4096, expiration=0`
4. configure RPM to use the key `sudo micro ~/.rpmmacros`
5. in the macros file add 
    ``` 
    %_signature gpg
    %_gpg_name <Name_of_repository> 
    ```
6. sign the package `rpm --addsign /var/www/html/<repo>/*.rpm`
7. export the public key `gpg --export -a "<Repo-Name>>" > /var/www/html/RPM-GPG-KEY-<repo>`

-----------
### Create repo file
1. `micro /var/www/html/<repo>.repo`
``` 
    [<repo-name>]
    name=(Kozlev's Repository) - <Repo-name>
    baseurl=https://<url-to-repo>/<repo-name>/
    enabled=1
    gpgcheck=1
    gpgkey=https://<url-to-repo>/RPM-GPG-KEY-<repo-name>
```

### Installation on client machine
1. `curl -o /etc/yum.repos.d/<repo-name>.repo <baseurl of repo>`
2. `sudo dnf makecache`
3. `sudo dnf install <package-name>`