# FolderLens Agent — başlangıç

[English quickstart](QUICKSTART.en.md) ·
[Ayrıntılı TR/EN kurulum](https://yavuzselimtech.tr/folderlens/agent/?lang=tr)

Bu paket Python kaynak kodudur; kurulu bir uygulama veya tek başına çalışan EXE
değildir. Python 3.11+ ve ilk bağımlılık kurulumu için internet erişimi gerekir.
Kendi bağlantınızı kurarsınız; pakette önceden verilmiş erişim, hesap, API anahtarı
veya tünel profili bulunmaz.

1. ZIP'i açın. İçindeki `FolderLens` klasöründe terminal açın.
2. `python folderlens.py --doctor` çalıştırın. Eksik bağımlılıklar bu klasörün
   `.venv` ortamına hazırlanır. Zengin PDF üretimi ayrıca tarayıcı
   kurulumu gerektirebilir; ayrıntılar `AGENT_GUIDE.md` içindedir.
3. Kendi çalışma klasörünüzü seçip ayarı üretin:

   ```powershell
   python folderlens.py --root "C:\calisma\projem" --print-config toml
   ```

4. Çıktıdaki Python `command` ve `args` alanlarını ChatGPT masaüstündeki MCP
   STDIO bağlantısına kaydedin. İstemci Python sürecini kendisi başlatır.
5. Sohbette FolderLens bağlantısını seçip `get_config` çağrısıyla cihazı,
   klasörü ve izinleri doğrulayın. İlk denemede dosya değiştirmeyin.

Varsayılan salt okunurdur. Dosya değişiklikleri için `--allow-write`, terminal
komutları için ayrıca `--allow-exec` ekleyip istemci argümanlarını güncelleyin.
Komutlar işletim sistemi hesabının yetkileriyle çalışır; `--root` komutları
klasöre hapsetmez. Kurulumdan ayrı bir çalışma klasörü seçin.

ChatGPT web/başka cihaz için kendi özel OpenAI Secure MCP Tunnel bağlantınızı
veya HTTPS/OAuth uç noktanızı kurmanız gerekir. Masaüstü STDIO ayarı webden
doğrudan erişilemez. Tünel anahtarınızı indirme sitesine veya sohbete yazmayın.
[Resmî tünel rehberi](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).

ZIP yanındaki `.sha256` dosyasını indirin. PowerShell'de
`Get-FileHash .\folderlens-agent.zip -Algorithm SHA256` ile karşılaştırın.
`RELEASE.json` paket kaynağının Git commit'ini ve her kaynak dosyanın SHA-256
değerini kaydeder. Sanal ortamlar, çalışma dosyaları ve özel durum kayıtları
pakete dahil değildir. Bağımlılıklar ilk çalıştırmada ayrıca indirilir.

Tarayıcıdan klasör paylaşımı için `python run.py` web sitesini açar; bu ayrı
yoldur. Tam ajan için `folderlens.py` kullanın. Gelişmiş araçlar ve sınırlar
`AGENT_GUIDE.md` içindedir. Eski `/AgentArm/` paylaşım URL'leri uyumluluk içindir;
ürün adı FolderLens'tir.
